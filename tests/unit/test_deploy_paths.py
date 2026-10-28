"""Every probe or healthcheck path in deploy files must be a real GET route."""

import re
from pathlib import Path
from typing import Iterable, List
from urllib.parse import urlsplit

import yaml

from hrms_plugin.server.app import app
from tests.support.routes import api_routes

DEPLOY = Path(__file__).parents[2] / "deploy"
_URL_PATH = re.compile(r"https?://[^/\s'\"]+(/[^\s'\")]*)")


def get_routes() -> set:
    return {r.path for r in api_routes(app.routes) if "GET" in r.methods}


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


# agy F3 — every deployment must supply the tenant registry the app requires at startup ----------

def _env_value(env_block, name):
    if isinstance(env_block, dict):
        return env_block.get(name)
    for item in env_block or []:
        if isinstance(item, str) and item.startswith(f"{name}="):
            return item.split("=", 1)[1]
        if isinstance(item, dict) and item.get("name") == name:
            return item.get("value")
    return None


def test_compose_mounts_tenant_registry_at_configured_path():
    api = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())["services"]["hrms-plugin-api"]
    path = _env_value(api.get("environment"), "HRMS_TENANT_REGISTRY")
    assert path, "HRMS_TENANT_REGISTRY not set for hrms-plugin-api"
    # host path may itself contain ":" (e.g. ${VAR:?msg}); target and mode are the last two fields
    mounts = [v.rsplit(":", 2) for v in api.get("volumes", []) if isinstance(v, str)]
    target = [m for m in mounts if len(m) >= 2 and m[1] == path]
    assert target, f"no volume mounted at {path}"
    assert target[0][0].startswith("${HRMS_TENANT_REGISTRY_FILE:?"), "host path must be required, not defaulted"
    assert target[0][-1] == "ro"


def test_k8s_mounts_tenant_registry_configmap_at_configured_path():
    docs = [d for d in yaml.safe_load_all((DEPLOY / "k8s" / "deployment.yaml").read_text()) if d]
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    spec = deployment["spec"]["template"]["spec"]
    container = spec["containers"][0]
    path = _env_value(container.get("env"), "HRMS_TENANT_REGISTRY")
    assert path, "HRMS_TENANT_REGISTRY not set in container env"
    mount = next(m for m in container.get("volumeMounts", []) if path.startswith(m["mountPath"].rstrip("/") + "/"))
    assert mount.get("readOnly") is True
    volume = next(v for v in spec.get("volumes", []) if v["name"] == mount["name"])
    assert volume["configMap"]["name"] == "hrms-plugin-tenant-registry"
    assert path.rsplit("/", 1)[1] in {i["path"] for i in volume["configMap"]["items"]}
