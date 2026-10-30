"""Unit tests for Task K3: Disclosure key generator and CLI subcommand."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import hrms_plugin.governance.disclosure as disclosure
from hrms_plugin.cli import main
from hrms_plugin.governance.disclosure import generate_key_file


def test_generate_key_file_creates_32_bytes_mode_600(tmp_path: Path):
    """Positive test: generate_key_file creates a 32-byte file with 0o600 permissions."""
    target = tmp_path / "secret.key"
    res = generate_key_file(target)
    assert res == target
    assert target.is_file()
    assert target.stat().st_size == 32
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_generate_key_file_from_string_path(tmp_path: Path):
    """Boundary test: generate_key_file accepts string path and returns Path."""
    target_str = str(tmp_path / "str_path.key")
    res = generate_key_file(target_str)
    assert isinstance(res, Path)
    assert res == Path(target_str)
    assert res.is_file()
    assert res.stat().st_size == 32


def test_generate_key_file_two_calls_give_different_keys(tmp_path: Path):
    """Positive test: subsequent calls generate cryptographically independent random keys."""
    key1 = tmp_path / "k1.key"
    key2 = tmp_path / "k2.key"
    generate_key_file(key1)
    generate_key_file(key2)
    assert key1.read_bytes() != key2.read_bytes()


def test_generate_key_file_accepted_by_disclosure_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Integration test: generated key file is accepted by is_configured and get_disclosure_key."""
    target = tmp_path / "active_disclosure.key"
    generate_key_file(target)
    monkeypatch.setenv("HRMS_DISCLOSURE_KEY_FILE", str(target))
    assert disclosure.is_configured() is True
    assert disclosure.get_disclosure_key() == target.read_bytes()


def test_generate_key_file_existing_file_raises_and_contents_unchanged(tmp_path: Path):
    """Negative test: existing file raises FileExistsError and preserves original content."""
    target = tmp_path / "existing.key"
    original_data = b"preexisting-secret-data-unchanged"
    target.write_bytes(original_data)

    with pytest.raises(FileExistsError):
        generate_key_file(target)

    assert target.read_bytes() == original_data


def test_generate_key_file_missing_parent_raises_file_not_found(tmp_path: Path):
    """Negative test: missing parent directory raises FileNotFoundError without creating file."""
    target = tmp_path / "missing_dir" / "secret.key"
    with pytest.raises(FileNotFoundError):
        generate_key_file(target)
    assert not target.exists()


def test_generate_key_file_umask_safety(tmp_path: Path):
    """Boundary test: 0o600 mode is guaranteed even when umask is permissive (0o000)."""
    old_umask = os.umask(0o000)
    try:
        target = tmp_path / "umask_zero.key"
        generate_key_file(target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    finally:
        os.umask(old_umask)


def test_cli_gen_disclosure_key_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Positive test: CLI gen-disclosure-key creates file, prints expected message and exits 0."""
    target = tmp_path / "cli_generated.key"
    code = main(["gen-disclosure-key", str(target)])
    assert code == 0
    captured = capsys.readouterr()

    expected_msg = (
        f"Disclosure key written to {target} (32 bytes, mode 600). "
        f"Set HRMS_DISCLOSURE_KEY_FILE={target.resolve()}."
    )
    assert expected_msg in captured.out
    assert captured.err == ""

    # Verify file contents and ensure key bytes (hex) are never leaked to stdout or stderr
    assert target.is_file()
    key_bytes = target.read_bytes()
    assert len(key_bytes) == 32
    assert key_bytes.hex() not in captured.out
    assert key_bytes.hex() not in captured.err


def test_cli_gen_disclosure_key_existing_file_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Negative test: CLI on existing file prints error to stderr and exits 1 without overwriting."""
    target = tmp_path / "cli_existing.key"
    original_data = b"do-not-overwrite-this-key-content"
    target.write_bytes(original_data)

    code = main(["gen-disclosure-key", str(target)])
    assert code == 1
    captured = capsys.readouterr()

    expected_err = (
        f"Error: '{target}' already exists; delete it first to rotate (old disclosure fingerprints stop matching).\n"
    )
    assert captured.err == expected_err
    assert captured.out == ""
    assert target.read_bytes() == original_data


def test_cli_gen_disclosure_key_missing_parent_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Negative test: CLI with non-existent parent directory prints error to stderr and exits 1."""
    target = tmp_path / "no_such_folder" / "secret.key"
    code = main(["gen-disclosure-key", str(target)])
    assert code == 1
    captured = capsys.readouterr()
    assert "Error:" in captured.err
    assert not target.exists()
