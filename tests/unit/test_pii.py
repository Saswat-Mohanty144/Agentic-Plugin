"""PII detectors, request-scoped sessions and stream restoration (piece C §3.1, §4)."""

import re

import pytest

from hrms_plugin.security.pii import PiiSession, StreamRestorer, detect

AADHAAR = "234567890124"          # Verhoeff-valid
AADHAAR_BAD = "234567890125"      # checksum off by one
EMIRATES_ID = "784199012345676"   # Luhn-valid
TOKEN_RE = re.compile(r"\[PII_[A-Z_]+_\d+_[0-9a-f]{8}\]")


def kinds(text):
    return {kind for kind, _start, _end in detect(text)}


# Detectors: positive ------------------------------------------------------------

@pytest.mark.parametrize("text,kind", [
    (f"id {AADHAAR} ok", "AADHAAR"),
    ("id 2345 6789 0124 ok", "AADHAAR"),
    ("id 2345-6789-0124 ok", "AADHAAR"),
    ("pan ABCPE1234F", "PAN"),
    (f"eid {EMIRATES_ID}", "EMIRATES_ID"),
    ("eid 784-1990-1234567-6", "EMIRATES_ID"),
    ("ssn 123-45-6789", "SSN"),
    ("iban GB82 WEST 1234 5698 7654 32", "IBAN"),
    ("iban AE070331234567890123456", "IBAN"),
    ("iban NO9386011117947", "IBAN"),                         # 15 chars, shortest real format
    ("iban GB16AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "IBAN"),       # 34 chars, longest allowed
    ("card 4111 1111 1111 1111", "CARD"),
    ("mail a@b.co", "EMAIL"),
    ("call +971 50 123 4567", "PHONE"),
    ("call 9876543210", "PHONE"),
    ("call +123456789012345", "PHONE"),                        # 15 digits
])
def test_detector_positive(text, kind):
    assert kind in kinds(text)


# Detectors: negative / boundary -------------------------------------------------------------

@pytest.mark.parametrize("text,kind", [
    (f"id {AADHAAR_BAD}", "AADHAAR"),
    ("id 134567890124", "AADHAAR"),          # first digit 1
    ("id 23456789012", "AADHAAR"),           # 11 digits
    ("id 2345678901245", "AADHAAR"),         # 13 digits
    ("ref 9" + AADHAAR + "9", "AADHAAR"),    # embedded in a longer number
    ("pan ABCXE1234F", "PAN"),               # invalid holder type
    ("pan abcpe1234f", "PAN"),
    ("eid 784199012345677", "EMIRATES_ID"),  # bad Luhn
    ("eid 785199012345676", "EMIRATES_ID"),
    ("ssn 000-45-6789", "SSN"), ("ssn 666-45-6789", "SSN"), ("ssn 912-45-6789", "SSN"),
    ("ssn 123-00-6789", "SSN"), ("ssn 123-45-0000", "SSN"),
    ("iban GB82WEST12345698765433", "IBAN"),
    ("card 4111 1111 1111 1112", "CARD"),
    ("card 411111111111", "CARD"),           # 12 digits
    ("card 41111111111111111111", "CARD"),   # 20 digits
    ("call 5876543210", "PHONE"),
    ("call +1234567", "PHONE"),              # 7 digits
    ("call +1234567890123456", "PHONE"),     # 16 digits
])
def test_detector_negative_and_boundary(text, kind):
    assert kind not in kinds(text)


def test_clean_text_has_no_detections():
    assert detect("Basic salary is 45000 for 12 months; CTC 1800000.") == []


# Session ----------------------------------------------------------------------------

def test_same_value_same_token_and_round_trip():
    s = PiiSession()
    red = s.redact_text(f"{AADHAAR} and again {AADHAAR}")
    tokens = TOKEN_RE.findall(red)
    assert len(tokens) == 2 and tokens[0] == tokens[1]
    assert AADHAAR not in red
    assert s.restore(red) == f"{AADHAAR} and again {AADHAAR}"


def test_learned_names_are_redacted_from_nested_records():
    s = PiiSession()
    s.learn({"employees": [{"id": "E1", "name": "Tariq Al-Mansoor", "basic_salary": 4000.0}],
             "manager": {"email": "boss@acme.test"}})
    red = s.redact_text("Report for Tariq Al-Mansoor sent to boss@acme.test, basic 4000.0")
    assert "Tariq" not in red and "boss@acme.test" not in red
    assert "4000.0" in red  # compensation stays (identity is tokenised) — decision C2


def test_identifier_field_names_are_normalised():
    s = PiiSession()
    s.learn({"EmployeeName": "Sunita Verma", "First-Name": "Ravi"})
    red = s.redact_text("Sunita Verma and Ravi")
    assert "Sunita" not in red and "Ravi" not in red


def test_longest_known_value_wins():
    s = PiiSession()
    s.learn([{"name": "Ali"}, {"name": "Ali Hassan"}])
    red = s.redact_text("Ali Hassan met Ali")
    assert "Hassan" not in red
    assert s.restore(red) == "Ali Hassan met Ali"


@pytest.mark.parametrize("value", ["", "A", "   "])
def test_trivial_values_are_not_learned(value):
    s = PiiSession()
    s.learn({"name": value})
    assert s.redact_text("A quick test") == "A quick test"


def test_other_sessions_tokens_are_not_restored():
    a, b = PiiSession(), PiiSession()
    token = TOKEN_RE.search(a.redact_text(AADHAAR)).group(0)
    assert b.restore(token) == token


def test_guessed_token_is_not_restored():
    s = PiiSession()
    s.redact_text(AADHAAR)
    assert s.restore("[PII_AADHAAR_1_00000000]") == "[PII_AADHAAR_1_00000000]"


def test_nonce_differs_between_sessions():
    assert TOKEN_RE.search(PiiSession().redact_text(AADHAAR)).group(0) != \
        TOKEN_RE.search(PiiSession().redact_text(AADHAAR)).group(0)


def test_restore_obj_walks_structures():
    s = PiiSession()
    s.learn({"name": "Rashid Al-Falasi"})
    token = TOKEN_RE.search(s.redact_text("Rashid Al-Falasi")).group(0)
    assert s.restore_obj({"a": [token, {"b": token}], "n": 3}) == \
        {"a": ["Rashid Al-Falasi", {"b": "Rashid Al-Falasi"}], "n": 3}


def test_find_pii_reports_kinds_not_values():
    s = PiiSession()
    s.learn({"name": "Rashid Al-Falasi"})
    assert set(s.find_pii(f"Rashid Al-Falasi {AADHAAR}")) == {"NAME", "AADHAAR"}
    assert s.find_pii(s.redact_text(f"Rashid Al-Falasi {AADHAAR}")) == []


# Stream restorer ------------------------------------------------------------------------

def _stream(session, chunks):
    r = StreamRestorer(session)
    out = [r.feed(c) for c in chunks]
    out.append(r.flush())
    return "".join(out)


def test_token_split_across_two_and_three_chunks():
    s = PiiSession()
    token = TOKEN_RE.search(s.redact_text(AADHAAR)).group(0)
    assert _stream(s, ["id ", token[:7], token[7:] + " ok"]) == f"id {AADHAAR} ok"
    assert _stream(s, ["id ", token[:3], token[3:12], token[12:]]) == f"id {AADHAAR}"


def test_unknown_token_like_text_passes_through():
    s = PiiSession()
    assert _stream(s, ["see [PII_X_1_deadbeef] end"]) == "see [PII_X_1_deadbeef] end"


def test_stream_ending_mid_token_flushes_verbatim():
    s = PiiSession()
    assert _stream(s, ["tail [PII_AAD"]) == "tail [PII_AAD"


def test_mod97_valid_but_14_chars_is_not_iban():
    assert "IBAN" not in kinds("iban GB95AAAAAAAAAA")


def test_known_values_respect_word_boundaries():
    s = PiiSession()
    s.learn({"name": "Ali"})
    assert s.redact_text("Alice and Salim met") == "Alice and Salim met"
    assert "Ali" not in s.redact_text("Ali met Alice").split(" met")[0]


# Decision C9: the grouped 4-4-4 Aadhaar format is redacted even with a bad checksum (typo'd Aadhaar is still PII);
# plain 12-digit numbers still require Verhoeff to avoid redacting account numbers and references.

@pytest.mark.parametrize("text", ["id 9876 5432 1098", "id 2345-6789-0125"])
def test_grouped_aadhaar_detected_even_with_bad_checksum(text):
    assert "AADHAAR" in kinds(text)


@pytest.mark.parametrize("text", ["id 2345 67890125", "id 2345-6789 0125"])  # mixed / partial grouping
def test_inconsistent_grouping_with_bad_checksum_not_aadhaar(text):
    assert "AADHAAR" not in kinds(text)


@pytest.mark.parametrize("text", ["card 4111 1111 1111 1111", "ref 1234 2345 6789 0124"])
def test_grouped_aadhaar_not_matched_inside_longer_grouped_numbers(text):
    assert "AADHAAR" not in kinds(text)
