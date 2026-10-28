"""Tests for `python -m hrms_plugin.server` startup validation."""

import os
import stat
import subprocess
import sys

import pytest
import uvicorn

from hrms_plugin.server import __main__ as entry


@pytest.fixture
def ran(monkeypatch):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    return calls


def test_defaults_bind_localhost_8000(ran, tmp_path):
    assert entry.main({"HRMS_DATA_DIR": str(tmp_path / "d")}) == 0
    assert ran == [{"host": "127.0.0.1", "port": 8000}]


def test_host_and_port_passed_through(ran, tmp_path):
    assert entry.main({"HOST": "0.0.0.0", "PORT": "9000", "HRMS_DATA_DIR": str(tmp_path)}) == 0
    assert ran == [{"host": "0.0.0.0", "port": 9000}]


@pytest.mark.parametrize("port", ["1", "65535"])
def test_port_boundaries_accepted(ran, tmp_path, port):
    assert entry.main({"PORT": port, "HRMS_DATA_DIR": str(tmp_path)}) == 0
    assert ran[0]["port"] == int(port)


@pytest.mark.parametrize("port", ["0", "65536", "-1", "abc", "", "8000.5", " 80"])
def test_invalid_port_exits_without_running(ran, tmp_path, capsys, port):
    assert entry.main({"PORT": port, "HRMS_DATA_DIR": str(tmp_path)}) != 0
    assert "PORT" in capsys.readouterr().err
    assert ran == []


def test_missing_data_dir_is_created_with_0700(ran, tmp_path):
    target = tmp_path / "new" / "data"
    assert entry.main({"HRMS_DATA_DIR": str(target)}) == 0
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_read_only_data_dir_exits(ran, tmp_path, capsys):
    target = tmp_path / "ro"
    target.mkdir()
    target.chmod(0o500)
    try:
        assert entry.main({"HRMS_DATA_DIR": str(target)}) != 0
        assert str(target) in capsys.readouterr().err
        assert ran == []
    finally:
        target.chmod(0o700)


def test_data_dir_that_is_a_file_exits(ran, tmp_path):
    target = tmp_path / "file"
    target.write_text("x")
    assert entry.main({"HRMS_DATA_DIR": str(target)}) != 0
    assert ran == []


def test_importing_mcp_server_does_not_build_the_app():
    env = {k: v for k, v in os.environ.items() if k != "HRMS_TENANT_REGISTRY"}
    code = "import hrms_plugin.server.mcp_server, sys; sys.exit('hrms_plugin.server.app' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], env=env).returncode == 0
