"""Every probe or healthcheck path in deploy files must be a real GET route."""

import re
from pathlib import Path
from typing import Iterable, List
from urllib.parse import urlsplit

import yaml

from hrms_plugin.server.app import app

DEPLOY = Path(__file__).parents[2] / "deploy"
_URL_PATH = re.compile(r"https?://[^/\s'\"]+(/[^\s'\")]*)")


def get_routes() -> set:
    return {r.path for r in app.routes if "GET" in getattr(r, "methods", set())}


def compose_paths(doc: dict) -> List[str]:
    paths = []
    for service in (doc.get("services") or {}).values():
        test = (service.get("healthcheck") or {}).get("test") or []
        paths += _URL_PATH.findall(" ".join(test) if isinstance(test, list) else str(test))
    return paths


def k8s_paths(docs: Iterable[dict]) -> List[str]:
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("readinessProbe", "livenessProbe", "startupProbe"):
                    path = ((value or {}).get("httpGet") or {}).get("path")
                    if path:
                        found.append(path)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for d in docs:
        walk(d)
    return found


def unknown_paths(paths: Iterable[str]) -> List[str]:
    routes = get_routes()
    return [p for p in paths if urlsplit(p).path not in routes]


def test_deploy_files_reference_real_routes():
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())
    k8s = list(yaml.safe_load_all((DEPLOY / "k8s" / "deployment.yaml").read_text()))
    paths = compose_paths(compose) + k8s_paths(k8s)
    assert len(paths) >= 3  # compose healthcheck + k8s readiness + liveness
    assert unknown_paths(paths) == []


def test_unknown_path_is_reported():
    doc = {"services": {"api": {"healthcheck": {"test": ["CMD", "curl", "http://localhost:8000/health"]}}}}
    assert unknown_paths(compose_paths(doc)) == ["/health"]


def test_query_string_is_ignored_when_matching():
    assert unknown_paths(["/healthz?x=1"]) == []
