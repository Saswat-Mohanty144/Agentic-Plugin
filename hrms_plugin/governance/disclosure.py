"""Disclosure index and reference generation (K2)."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hrms_plugin.security import pii

VALID_KINDS = frozenset({
    "AADHAAR", "PAN", "EMIRATES_ID", "SSN", "CARD", "EMAIL", "PHONE", "IBAN",
    "NAME", "ADDRESS", "DOB", "PASSPORT", "NATIONAL_ID", "BANK_ACCOUNT", "COMPENSATION",
})

_SPACE_HYPHEN_KINDS = frozenset({
    "AADHAAR", "PAN", "CARD", "IBAN", "SSN", "EMIRATES_ID", "PHONE",
})

_KEY_CACHE: Dict[Tuple[str, int, int], bytes] = {}


def get_disclosure_key() -> Optional[bytes]:
    """Read the disclosure secret key from the path in HRMS_DISCLOSURE_KEY_FILE lazily, cached by stat."""
    path_str = os.environ.get("HRMS_DISCLOSURE_KEY_FILE", "").strip()
    if not path_str:
        return None
    try:
        p = Path(path_str)
        st = p.stat()
        if st.st_size < 32:
            return None
        cache_key = (path_str, st.st_mtime_ns, st.st_size)
        cached = _KEY_CACHE.get(cache_key)
        if cached is not None:
            return cached
        data = p.read_bytes()
        if len(data) < 32:
            return None
        _KEY_CACHE[cache_key] = data
        return data
    except OSError:
        return None


def is_configured() -> bool:
    """Return True if a valid disclosure key (>=32 bytes) is configured."""
    return get_disclosure_key() is not None


def canonical(kind: str, value: str) -> str:
    """Canonicalise a value: NFKC, casefold, strip; remove spaces and hyphens for listed kinds only."""
    text = pii.normalise(str(value)).casefold().strip()
    if kind.upper() in _SPACE_HYPHEN_KINDS:
        text = text.replace(" ", "").replace("-", "")
    return text


def compute_ref(tenant_id: str, kind: str, value: str, key: Optional[bytes] = None) -> Optional[str]:
    """Compute hmac-sha256 reference for tenant_id, kind, and canonical value."""
    if key is None:
        key = get_disclosure_key()
    if key is None:
        return None
    canonical_val = canonical(kind, value)
    msg = f"{tenant_id}\x00{kind.upper()}\x00{canonical_val}".encode("utf-8")
    digest = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def extract_disclosed_refs(tenant_id: str, data: Any) -> List[Dict[str, str]]:
    """Walk JSON output to detect string leaves and identifier key scalars, returning DISCLOSED refs."""
    key = get_disclosure_key()
    if key is None:
        return []

    collected: List[Tuple[str, str]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                k_str = str(k)
                kind = pii.identifier_kind(k_str)
                if kind is not None and not isinstance(v, bool) and isinstance(v, (str, int, float, Decimal)):
                    collected.append((kind, str(v)))
                _walk(v)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)
        elif isinstance(node, str):
            norm = pii.normalise(node)
            for detected_kind, start, end in pii.detect(norm):
                collected.append((detected_kind, norm[start:end]))

    _walk(data)

    out: List[Dict[str, str]] = []
    seen_refs = set()
    for kind, val in collected:
        ref = compute_ref(tenant_id, kind, val, key=key)
        if ref and ref not in seen_refs:
            seen_refs.add(ref)
            out.append({"ref": ref, "kind": kind, "outcome": "DISCLOSED"})
    return out


def generate_key_file(path: str | Path) -> Path:
    """Generate and write a 32-byte disclosure secret key file atomically with mode 0o600."""
    p = Path(path)
    if not p.parent.exists():
        raise FileNotFoundError(f"Parent directory does not exist: {p.parent}")
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        data = secrets.token_bytes(32)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        fd = -1
    finally:
        if fd != -1:
            os.close(fd)
    os.chmod(p, 0o600)
    return p

