"""agy piece-C findings F3-F8, F10: evasion attempts that must now be caught."""

import pytest

from hrms_plugin.security.pii import PiiSession, detect, normalise


def kinds(text):
    return {k for k, _s, _e in detect(normalise(text))}


@pytest.mark.parametrize("text,kind", [
    ("iban: ae070331234567890123456", "IBAN"),                 # F4 lowercase
    ("iban gb82 west 1234 5698 7654 32", "IBAN"),
    ("Call 98765 43210", "PHONE"),                             # F5 spaced Indian mobile
    ("Call 98765-43210", "PHONE"),
    ("Call +91 98765 43210", "PHONE"),
    ("Aadhaar 2345 6789-0124", "AADHAAR"),                     # F6 mixed separators, valid checksum
    ("Aadhaar २३४५६७८९०१२४", "AADHAAR"),                       # F7 Devanagari digits
    ("Aadhaar ２３４５６７８９０１２４", "AADHAAR"),              # fullwidth digits
    ("Aadhaar 2345​6789​0124", "AADHAAR"),           # F8 zero-width spaces
    ("pan abcpe1234f", "PAN"),                                 # F10 lowercase PAN
    ("pan AbCpE1234f", "PAN"),
])
def test_evasions_are_detected(text, kind):
    assert kind in kinds(text)


@pytest.mark.parametrize("text,kind", [
    ("Aadhaar 2345 6789-0125", "AADHAAR"),     # mixed separators AND bad checksum: still not Aadhaar
    ("Call 58765 43210", "PHONE"),             # Indian mobiles start 6-9
    ("ref 98765 432109", "PHONE"),             # 11 digits
    ("pan abcxe1234f", "PAN"),                 # invalid holder type, any case
])
def test_hardened_detectors_keep_their_negatives(text, kind):
    assert kind not in kinds(text)


def test_normalise_strips_invisible_chars_and_maps_digits():
    assert normalise("A​B⁠C ２३") == "ABC 23"


def test_redaction_catches_normalised_forms():
    s = PiiSession()
    out = s.redact_text("id २३४५६७८९०१२४ and 2345​6789​0124")
    assert "२" not in out and "2345" not in out


def test_learned_names_match_case_insensitively():                  # F3
    s = PiiSession()
    s.learn({"name": "Tariq Al-Mansoor"})
    red = s.redact_text("Calculate gratuity for tariq al-mansoor and TARIQ AL-MANSOOR")
    assert "tariq" not in red.lower()
    assert s.find_pii("for tariq al-mansoor") == ["NAME"]
    assert s.restore(red) == "Calculate gratuity for tariq al-mansoor and TARIQ AL-MANSOOR"


def test_learned_name_with_zero_width_inside_is_caught():
    s = PiiSession()
    s.learn({"name": "Tariq Al-Mansoor"})
    assert "Tariq" not in s.redact_text("for Tar​iq Al-Mansoor")
