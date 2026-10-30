"""Run the HRMS plugin HTTP server: `python -m hrms_plugin.server`."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Optional


def _parse_port(raw: str) -> int:
    if not raw.isdigit():
        raise ValueError(raw)
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ValueError(raw)
    return port


def _ensure_data_dir(raw: str) -> Path:
    path = Path(raw)
    if path.exists() and not path.is_dir():
        raise ValueError(f"HRMS_DATA_DIR {path} exists and is not a directory")
    if not path.exists():
        path.mkdir(parents=True)
        path.chmod(0o700)
    if not os.access(path, os.W_OK | os.X_OK):
        raise ValueError(f"HRMS_DATA_DIR {path} is not writable")
    return path


def main(env: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if env is None else env
    host = env.get("HOST", "127.0.0.1")
    raw_port = env.get("PORT", "8000")
    try:
        port = _parse_port(raw_port)
    except ValueError:
        print(f"Invalid PORT {raw_port!r}: must be an integer between 1 and 65535", file=sys.stderr)
        return 2
    try:
        _ensure_data_dir((env.get("HRMS_DATA_DIR") or "").strip() or ".hrms_data")
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    import uvicorn

    from hrms_plugin.server.app import app

    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
