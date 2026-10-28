"""Request-scoped PII detection, tokenisation and restoration for everything sent to an LLM (piece C).

Tokens look like ``[PII_<KIND>_<n>_<nonce>]``. They exist only inside one :class:`PiiSession` (one request),
are never persisted, and only the session that minted a token can restore it.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

Match = Tuple[str, int, int]  # (kind, start, end)

# --- checksum helpers -------------------------------------------------------------------------------

_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _verhoeff_valid(number: str) -> bool:
    check = 0
    for i, ch in enumerate(reversed(number)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][int(ch)]]
    return check == 0


def _luhn_valid(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _iban_valid(compact: str) -> bool:
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


def _aadhaar_like(text: str) -> bool:
    """Valid Verhoeff always counts; consistently grouped 4-4-4 counts even with a typo (C9)."""
    if _verhoeff_valid(_digits(text)):
        return True
    separators = re.findall(r"[ -]", text)
    return len(separators) == 2 and separators[0] == separators[1]


def _ssn_valid(text: str) -> bool:
    area, group, serial = text.split("-")
    return area not in ("000", "666") and not area.startswith("9") and group != "00" and serial != "0000"


# --- normalisation (agy F7/F8) -----------------------------------------------------------------------


def normalise(text: str) -> str:
    """NFKC, drop invisible format characters (zero-width, soft hyphen, BOM), map any decimal digit to ASCII.

    Everything sent to an LLM is normalised first, so detectors can't be evaded with look-alike characters.
    """
    out = []
    for ch in unicodedata.normalize("NFKC", text):
        if unicodedata.category(ch) == "Cf":
            continue
        if ch.isdecimal() and not ch.isascii():
            ch = str(unicodedata.decimal(ch))
        out.append(ch)
    return "".join(out)


# --- detectors -------------------------------------------------------------------------------------

_ND = r"(?<![\dA-Za-z])"   # not preceded by a digit or letter
_NA = r"(?![\dA-Za-z])"    # not followed by a digit or letter

_SIMPLE: List[Tuple[str, "re.Pattern[str]", Optional[Callable[[str], bool]]]] = [
    # Grouped 4-4-4 is the printed Aadhaar format: redact even with a checksum typo (decision C9).
    ("AADHAAR", re.compile(r"(?<!\d[ -])" + _ND + r"[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![ -]?\d)" + _NA), _aadhaar_like),
    ("PAN", re.compile(_ND + r"[A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z]" + _NA, re.IGNORECASE), None),
    ("EMIRATES_ID", re.compile(_ND + r"784-?\d{4}-?\d{7}-?\d" + _NA), lambda m: _luhn_valid(_digits(m))),
    ("SSN", re.compile(_ND + r"\d{3}-\d{2}-\d{4}" + _NA), _ssn_valid),
    ("CARD", re.compile(_ND + r"\d(?:[ -]?\d){12,18}" + _NA), lambda m: _luhn_valid(_digits(m))),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), None),
    ("PHONE", re.compile(r"(?<![\w+])\+\d(?:[ -]?\d){7,14}" + _NA), None),
    ("PHONE", re.compile(_ND + r"(?:\+91[ -]?)?[6-9]\d{4}[ -]?\d{5}" + _NA), None),
]
_IBAN_START = re.compile(_ND + r"[A-Za-z]{2}\d{2}")
_PRIORITY = {"AADHAAR": 0, "PAN": 0, "EMIRATES_ID": 0, "SSN": 0, "IBAN": 1, "CARD": 2, "EMAIL": 3, "PHONE": 4}


def _iban_matches(text: str) -> List[Match]:
    found: List[Match] = []
    for start_match in _IBAN_START.finditer(text):
        start = start_match.start()
        # Collect candidate end positions: alphanumerics, allowing single spaces between them.
        ends: List[Tuple[int, int]] = []  # (end_index, compact_length)
        i, compact = start, 0
        while i < len(text) and compact < 34:
            ch = text[i]
            if ch.isascii() and ch.isalnum():
                compact += 1
                i += 1
                ends.append((i, compact))
            elif ch == " " and i + 1 < len(text) and text[i + 1].isascii() and text[i + 1].isalnum():
                i += 1
            else:
                break
        for end, length in reversed(ends):
            if 15 <= length <= 34 and not (end < len(text) and text[end].isalnum()):
                if _iban_valid(text[start:end].replace(" ", "").upper()):
                    found.append(("IBAN", start, end))
                    break
    return found


def detect(text: str) -> List[Match]:
    """Validated, non-overlapping PII matches in ``text`` (more specific kinds win overlaps).

    Callers pass :func:`normalise`-d text; spans refer to that text.
    """
    candidates: List[Match] = list(_iban_matches(text))
    for kind, pattern, validator in _SIMPLE:
        for m in pattern.finditer(text):
            if validator is None or validator(m.group(0)):
                candidates.append((kind, m.start(), m.end()))
    candidates.sort(key=lambda c: (c[1], _PRIORITY[c[0]], -(c[2] - c[1])))
    chosen: List[Match] = []
    for cand in candidates:
        if all(cand[2] <= c[1] or cand[1] >= c[2] for c in chosen):
            chosen.append(cand)
    return sorted(chosen, key=lambda c: c[1])


# --- identifier fields -------------------------------------------------------------------------------

_FIELD_KINDS: Dict[str, str] = {
    **{k: "NAME" for k in ("name", "first_name", "last_name", "full_name", "employee_name", "candidate_name")},
    **{k: "EMAIL" for k in ("email", "work_email", "personal_email")},
    **{k: "PHONE" for k in ("phone", "mobile", "phone_number")},
    "address": "ADDRESS",
    "dob": "DOB",
    "date_of_birth": "DOB",
    "aadhaar": "AADHAAR",
    "aadhaar_number": "AADHAAR",
    "pan": "PAN",
    "pan_number": "PAN",
    "ssn": "SSN",
    "emirates_id": "EMIRATES_ID",
    "passport": "PASSPORT",
    "passport_number": "PASSPORT",
    "national_id": "NATIONAL_ID",
    **{k: "BANK_ACCOUNT" for k in ("bank_account", "account_number", "iban", "card_number")},
}


def _normalise_key(key: str) -> str:
    key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def identifier_kind(key: str) -> Optional[str]:
    return _FIELD_KINDS.get(_normalise_key(key))


# --- session ------------------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\[PII_[A-Z_]+?_\d+_[0-9a-f]{8}\]")
_TOKEN_PREFIX = "[PII_"
_MAX_TOKEN_LEN = 64


class PiiEgressBlocked(Exception):
    """Raw PII would have left the plugin; the LLM call was not made."""

    def __init__(self, kinds: Iterable[str]):
        self.kinds = sorted(set(kinds))
        super().__init__(",".join(self.kinds))


class PiiSession:
    """Tokenises PII for one request. Tokens are unpredictable (per-session nonce) and never persisted."""

    def __init__(self) -> None:
        self._nonce = secrets.token_hex(4)
        self._known: Dict[str, str] = {}          # casefolded value -> kind (learned from identifier fields)
        self._token_for: Dict[str, str] = {}      # raw value -> token
        self._value_for: Dict[str, str] = {}      # token -> raw value
        self._counters: Dict[str, int] = {}

    # learning -------------------------------------------------------------------------------------
    def learn(self, obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                kind = identifier_kind(str(key))
                if kind and isinstance(value, (str, int)) and not isinstance(value, bool):
                    text = normalise(str(value)).strip()
                    if len(text) >= 2:
                        self._known[text.casefold()] = kind
                else:
                    self.learn(value)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                self.learn(item)

    # tokenising -----------------------------------------------------------------------------------
    def _token(self, value: str, kind: str) -> str:
        token = self._token_for.get(value)
        if token is None:
            self._counters[kind] = self._counters.get(kind, 0) + 1
            token = f"[PII_{kind}_{self._counters[kind]}_{self._nonce}]"
            self._token_for[value] = token
            self._value_for[token] = value
        return token

    def _known_pattern(self) -> Optional["re.Pattern[str]"]:
        if not self._known:
            return None
        values = sorted(self._known, key=len, reverse=True)
        return re.compile(r"(?<!\w)(" + "|".join(re.escape(v) for v in values) + r")(?!\w)", re.IGNORECASE)

    def redact_text(self, text: str) -> str:
        if not text:
            return text
        text = normalise(text)
        pattern = self._known_pattern()
        if pattern is not None:
            text = pattern.sub(lambda m: self._token(m.group(1), self._known[m.group(1).casefold()]), text)
        out, last = [], 0
        for kind, start, end in detect(text):
            if self._inside_token(text, start):
                continue
            out.append(text[last:start])
            out.append(self._token(text[start:end], kind))
            last = end
        out.append(text[last:])
        return "".join(out)

    @staticmethod
    def _inside_token(text: str, index: int) -> bool:
        return any(m.start() <= index < m.end() for m in _TOKEN_RE.finditer(text))

    # restoring ------------------------------------------------------------------------------------
    def restore(self, text: str) -> str:
        if not text or not self._value_for:
            return text
        return _TOKEN_RE.sub(lambda m: self._value_for.get(m.group(0), m.group(0)), text)

    def restore_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.restore(obj)
        if isinstance(obj, dict):
            return {k: self.restore_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.restore_obj(v) for v in obj]
        return obj

    # checking -------------------------------------------------------------------------------------
    def find_pii(self, text: str) -> List[str]:
        if not text:
            return []
        text = normalise(text)
        found = set()
        pattern = self._known_pattern()
        if pattern is not None:
            found.update(self._known[m.group(1).casefold()] for m in pattern.finditer(text))
        for kind, start, _end in detect(text):
            if not self._inside_token(text, start):
                found.add(kind)
        return sorted(found)


class StreamRestorer:
    """Restores tokens in streamed text, holding back a trailing partial token until it completes."""

    def __init__(self, session: PiiSession):
        self._session = session
        self._buffer = ""

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        cut = self._buffer.rfind("[")
        if cut != -1 and "]" not in self._buffer[cut:]:
            tail = self._buffer[cut:]
            if len(tail) < _MAX_TOKEN_LEN and (_TOKEN_PREFIX.startswith(tail) or tail.startswith(_TOKEN_PREFIX)):
                ready, self._buffer = self._buffer[:cut], tail
                return self._session.restore(ready)
        ready, self._buffer = self._buffer, ""
        return self._session.restore(ready)

    def flush(self) -> str:
        ready, self._buffer = self._buffer, ""
        return self._session.restore(ready)
