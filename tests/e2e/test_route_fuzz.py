"""Schema-driven fuzz of every HTTP route: hostile input must never produce a 5xx.

Independent of any reviewer's judgement: for each route in the OpenAPI document, every body field is replaced with
wrong-typed / boundary / hostile values and path parameters with hostile segments. Outbound network is forced to fail,
so upstream-unavailable handling is exercised too. Invariant: status < 500 (502/503/504 are allowed only as honest
upstream-failure answers, never 500).
"""

import httpx
import pytest

from hrms_plugin.server.app import create_app  # noqa: F401  (imported so collection fails loudly if the app breaks)

HOSTILE = [None, True, -1, 0, 10**20, 1.5, "", " ", "x" * 100_000, "\u0000", "' OR 1=1 --", "<script>", "../..",
           "%00", "١٢٣", [], [None], {}, {"$ref": "#"}, {"a": {"b": {"c": {"d": [1, {"e": None}]}}}}]
WHOLE_BODIES = [None, [], "text", 1, {}, {"unexpected": 1}]
PATH_VALUES = ["x", "a:b", "..", "%2e%2e", " ", "9" * 300, "-1", "0", "‮", "EMPLOYEE"]
UPSTREAM_STATUSES = {502, 503, 504}


def _resolve(spec, schema):
    while isinstance(schema, dict) and "$ref" in schema:
        node = spec
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        schema = node
    return schema or {}


def _cases(spec):
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            body_schema = _resolve(spec, (op.get("requestBody") or {}).get("content", {})
                                   .get("application/json", {}).get("schema"))
            props = list(body_schema.get("properties", {}))
            yield path, method, props


def _paths(path):
    if "{" not in path:
        return [path]
    out = []
    for value in PATH_VALUES:
        filled, rest = "", path
        while "{" in rest:
            pre, _, rest = rest.partition("{")
            _, _, rest = rest.partition("}")
            filled += pre + httpx.URL("http://h/").copy_with(path="/" + value).raw_path.decode()[1:]
        out.append(filled + rest)
    return out


def _bodies(props):
    yield from WHOLE_BODIES
    for prop in props:
        for value in HOSTILE:
            yield {prop: value}


@pytest.fixture
def fuzz_client(make_client, monkeypatch):
    async def no_network(self, request, *a, **kw):
        raise httpx.ConnectError("network disabled in fuzz", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    return make_client(raise_server_exceptions=False)


def test_no_route_returns_5xx_on_hostile_input(fuzz_client, bearer):
    headers = bearer(roles=["platform_admin"])
    spec = fuzz_client.app.openapi()  # /openapi.json is deliberately not served
    failures, sent = [], 0
    for path, method, props in _cases(spec):
        for url in _paths(path):
            bodies = list(_bodies(props)) if method in ("post", "put", "patch") else [None]
            for body in bodies:
                kwargs = {"headers": headers}
                if method in ("post", "put", "patch"):
                    kwargs["json"] = body
                res = fuzz_client.request(method.upper(), url, **kwargs)
                sent += 1
                if res.status_code >= 500 and res.status_code not in UPSTREAM_STATUSES:
                    failures.append(f"{method.upper()} {url[:80]} body={str(body)[:60]!r} -> {res.status_code}")
    assert sent > 500, sent
    assert not failures, f"{len(failures)} of {sent} requests returned 5xx:\n" + "\n".join(sorted(set(failures))[:60])
