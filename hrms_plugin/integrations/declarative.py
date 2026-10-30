"""Declarative vendor profiles: a new host HRMS is onboarded with a YAML file, not code (piece I, decisions I1-I5).

A profile says how to list one canonical entity from a vendor (auth scheme, path, paging, envelope) and how each
canonical field is read from a vendor record. Values that do not convert, or enumerations the profile does not map,
reject that record (reported) instead of passing through.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Union
from urllib.parse import quote

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError, model_validator

from hrms_plugin.integrations.host_registry import HostConfig, HostRegistryError

PROFILE_DIR = Path(__file__).with_name("profiles")
MAX_PAGES = 50
REQUISITION_FIELDS = ("title", "department", "location", "headcount", "status", "description", "hiring_manager_ref")
# APPROVED = approved internally, not yet published; OPEN = published / actively hiring (agy I-F7).
REQUISITION_STATUSES = (
    "PENDING", "APPROVED", "OPEN", "REJECTED", "FILLED", "ON_HOLD", "CANCELLED", "DRAFT", "CLOSED"
)
INT_FIELDS = ("headcount",)  # every other canonical requisition field is text (agy I-F2)
# Path parameters a profile may name; values always come from the host registry (I2).
PATH_PARAMS = ("service_user_id",)
_PATH = r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$"
_TEMPLATE_PARAM = re.compile(r"\{([^{}]*)\}")
_POSITIVE_INT = re.compile(r"^[0-9]+$")


class ProfileError(Exception):
    """A vendor profile is missing or invalid."""


class ConnectorError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    source: StrictStr = Field(alias="from", pattern=_PATH)
    type: Literal["str", "int"] = "str"
    map: Optional[Dict[StrictStr, StrictStr]] = None
    transform: Optional[Literal["strip", "lower", "upper"]] = None
    required: Optional[StrictBool] = None

    @model_validator(mode="after")
    def _validate_transform(self) -> "FieldSpec":
        if self.transform is not None and self.type != "str":
            raise ValueError("transform is only supported for str fields")
        return self


class OffsetPaging(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["offset"]
    start_param: StrictStr
    size_param: StrictStr
    size: StrictInt = Field(ge=1, le=100)

    def param_names(self) -> List[str]:
        return [self.start_param, self.size_param]


class PagePaging(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["page"]
    page_param: StrictStr
    size_param: StrictStr
    size: StrictInt = Field(ge=1, le=100)
    total_pages_at: StrictStr = Field(pattern=_PATH)

    def param_names(self) -> List[str]:
        return [self.page_param, self.size_param]


class EntitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: StrictStr = Field(pattern=r"^/")
    request_fields: StrictBool = False
    items_at: StrictStr = Field(pattern=_PATH)
    pagination: Union[OffsetPaging, PagePaging] = Field(discriminator="kind")
    id_from: StrictStr = Field(pattern=_PATH)
    fields: Dict[Literal[REQUISITION_FIELDS], FieldSpec]  # type: ignore[valid-type]

    @model_validator(mode="after")
    def _consistent(self) -> "EntitySpec":
        for name in _TEMPLATE_PARAM.findall(self.path):
            if name not in PATH_PARAMS:
                raise ValueError(f"path parameter {{{name}}} is not allowed")
        for name, spec in self.fields.items():
            if spec.type != ("int" if name in INT_FIELDS else "str"):
                raise ValueError(f"{name} must be declared type {'int' if name in INT_FIELDS else 'str'}")
        params = self.pagination.param_names() + (["fields"] if self.request_fields else [])
        if len(params) != len(set(params)):
            raise ValueError(f"query parameter names collide: {params}")  # agy I-F11
        status = self.fields.get("status")
        if status is not None:
            if not status.map:
                raise ValueError("status needs a value map")
            unknown = set(status.map.values()) - set(REQUISITION_STATUSES)
            if unknown:
                raise ValueError(f"status maps to unknown canonical values {sorted(unknown)}")
        if self.request_fields:
            for spec in [*self.fields.values()]:
                if "." in spec.source:
                    raise ValueError("requested fields must be top-level")
            if "." in self.id_from:
                raise ValueError("requested fields must be top-level")
        return self


class McpEntitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: StrictStr = Field(pattern=r"^/")
    tool: StrictStr
    arguments: Optional[Dict[StrictStr, Any]] = None
    items_at: Optional[StrictStr] = Field(default=None, pattern=_PATH)
    cursor_param: Optional[StrictStr] = None
    next_cursor_at: Optional[StrictStr] = Field(default=None, pattern=_PATH)
    id_from: StrictStr = Field(pattern=_PATH)
    fields: Dict[Literal[REQUISITION_FIELDS], FieldSpec]  # type: ignore[valid-type]

    @model_validator(mode="after")
    def _consistent(self) -> "McpEntitySpec":
        for name in _TEMPLATE_PARAM.findall(self.path):
            if name not in PATH_PARAMS:
                raise ValueError(f"path parameter {{{name}}} is not allowed")
        for name, spec in self.fields.items():
            if spec.type != ("int" if name in INT_FIELDS else "str"):
                raise ValueError(f"{name} must be declared type {'int' if name in INT_FIELDS else 'str'}")
        status = self.fields.get("status")
        if status is not None:
            if not status.map:
                raise ValueError("status needs a value map")
            unknown = set(status.map.values()) - set(REQUISITION_STATUSES)
            if unknown:
                raise ValueError(f"status maps to unknown canonical values {sorted(unknown)}")
        if (self.cursor_param is None) != (self.next_cursor_at is None):
            raise ValueError("cursor_param and next_cursor_at must both be set if cursor paging is configured")
        return self


AnyEntitySpec = Union[EntitySpec, McpEntitySpec]


class VendorProfileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal["rest", "mcp"] = "rest"
    vendor: Literal["frappe", "iceipts"]
    auth: Literal["frappe_token", "bearer"]
    entities: Dict[Literal["REQUISITION"], AnyEntitySpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_protocol(self) -> "VendorProfileSpec":
        if self.protocol == "mcp":
            if self.auth != "bearer":
                raise ValueError("Bearer auth only for mcp")
            for name, entity in self.entities.items():
                if not isinstance(entity, McpEntitySpec):
                    raise ValueError(f"entity {name} must be McpEntitySpec when protocol is mcp")
        elif self.protocol == "rest":
            for name, entity in self.entities.items():
                if not isinstance(entity, EntitySpec):
                    raise ValueError(f"entity {name} must be EntitySpec when protocol is rest")
        return self


def parse_profile(data: Any) -> VendorProfileSpec:
    try:
        return VendorProfileSpec.model_validate(data)
    except ValidationError as exc:
        raise ProfileError(str(exc)) from exc


def load_profile(vendor: str) -> VendorProfileSpec:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", vendor or ""):
        raise ProfileError(f"invalid vendor name {vendor!r}")
    path = PROFILE_DIR / f"{vendor}.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"no usable profile for vendor {vendor!r}") from exc
    return parse_profile(data)


@dataclass
class ListResult:
    records: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)  # raw host rows, for the shape check only


_JSON_TYPE = {bool: "bool", int: "int", float: "float", str: "str", list: "list", dict: "dict"}


def _has_path(obj: Any, path: str) -> bool:
    for part in path.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return False
        obj = obj[part]
    return True


def observed_shape(rows: List[Dict[str, Any]], spec: AnyEntitySpec) -> Dict[str, Dict[str, Any]]:
    """For each path the profile reads: is the key present in any row, and which non-null JSON types it carries (QE)."""
    paths = sorted({spec.id_from, *(f.source for f in spec.fields.values())})
    shape: Dict[str, Dict[str, Any]] = {}
    for path in paths:
        values = [_get(r, path) for r in rows if _has_path(r, path)]
        types = sorted({_JSON_TYPE.get(type(v), type(v).__name__) for v in values if v is not None})
        shape[path] = {"present": bool(values) or any(_has_path(r, path) for r in rows), "types": types}
    return shape


def shape_fingerprint(shape: Dict[str, Dict[str, Any]]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()


def shape_changes(approved: Dict[str, Dict[str, Any]], observed: Dict[str, Dict[str, Any]]) -> List[str]:
    """Paths whose presence changed or that carry a type the approved shape never had. Nulls are never drift."""
    changed = []
    for path in sorted(set(approved) | set(observed)):
        a = approved.get(path, {"present": False, "types": []})
        o = observed.get(path, {"present": False, "types": []})
        if a["present"] != o["present"] or not set(o["types"]) <= set(a["types"]):
            changed.append(path)
    return changed


class _Rejected(Exception):
    def __init__(self, error: str, field_name: str):
        self.error = error
        self.field_name = field_name


def _get(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _default_transport() -> Optional[httpx.AsyncBaseTransport]:
    return None  # real network; tests substitute a mock transport


def _parse_sse(text: str, expected_id: Any) -> Optional[Dict[str, Any]]:
    events = re.split(r"(?:\r?\n){2,}", text)
    for event in events:
        data_parts = []
        for line in event.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    data_parts.append(payload)
        if data_parts:
            combined = "\n".join(data_parts)
            try:
                obj = json.loads(combined)
                if isinstance(obj, dict) and (
                    obj.get("id") == expected_id or str(obj.get("id")) == str(expected_id)
                ):
                    return obj
            except Exception:
                continue
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    obj = json.loads(payload)
                    if isinstance(obj, dict) and (
                        obj.get("id") == expected_id or str(obj.get("id")) == str(expected_id)
                    ):
                        return obj
                except Exception:
                    continue
    return None


def _parse_jsonrpc_response(response: httpx.Response, expected_id: Any) -> Dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        obj = _parse_sse(response.text, expected_id)
        if obj is None:
            raise ConnectorError("HOST_ERROR", "no matching response")
        return obj
    try:
        data = response.json()
    except Exception:
        raise ConnectorError("INCONSISTENT_PAYLOAD", "not JSON") from None
    if not isinstance(data, dict):
        raise ConnectorError("INCONSISTENT_PAYLOAD", "not JSON")
    return data


class RestTransport:
    async def fetch_page(
        self, client: httpx.AsyncClient, url: str, auth_header: str, spec: EntitySpec, page_index: int
    ) -> tuple[List[Dict[str, Any]], bool]:
        paging = spec.pagination
        params: Dict[str, str] = {}
        if spec.request_fields:
            wanted = [spec.id_from] + [f.source for f in spec.fields.values()]
            params["fields"] = json.dumps(list(dict.fromkeys(wanted)))
        if isinstance(paging, OffsetPaging):
            params[paging.start_param] = str(page_index * paging.size)
        else:
            params[paging.page_param] = str(page_index + 1)
        params[paging.size_param] = str(paging.size)
        headers = {"Authorization": auth_header, "Accept": "application/json"}
        try:
            response = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError:
            raise ConnectorError("HOST_ERROR", "request failed") from None
        if response.status_code != 200:
            raise ConnectorError("HOST_ERROR", f"status {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise ConnectorError("INCONSISTENT_PAYLOAD", "not JSON") from None
        rows = _get(body, spec.items_at)
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ConnectorError("INCONSISTENT_PAYLOAD", f"{spec.items_at} is not a list of objects")
        if isinstance(paging, OffsetPaging):
            return rows, len(rows) == paging.size
        total = _get(body, paging.total_pages_at)
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise ConnectorError("INCONSISTENT_PAYLOAD", f"{paging.total_pages_at} is not a page count")
        return rows, page_index + 1 < total


class McpTransport:
    def __init__(self) -> None:
        self._session_id: Optional[str] = None
        self._protocol_version: str = "2025-06-18"
        self._initialized: bool = False
        self._next_cursor: Optional[str] = None
        self._req_id: int = 0

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def fetch_page(
        self, client: httpx.AsyncClient, url: str, auth_header: str, spec: McpEntitySpec, page_index: int
    ) -> tuple[List[Dict[str, Any]], bool]:
        if not self._initialized:
            await self._initialize(client, url, auth_header)
            self._initialized = True

        return await self._call_tool(client, url, auth_header, spec, page_index)

    async def _initialize(self, client: httpx.AsyncClient, url: str, auth_header: str) -> None:
        req_id = self._next_id()
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "hrms-agentic-plugin", "version": "2.0.0"},
            },
        }
        headers = {
            "Authorization": auth_header,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError:
            raise ConnectorError("HOST_ERROR", "request failed") from None

        if response.status_code != 200:
            raise ConnectorError("HOST_ERROR", f"status {response.status_code}")

        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id

        data = _parse_jsonrpc_response(response, req_id)
        if "error" in data:
            raise ConnectorError("HOST_ERROR")

        result = data.get("result")
        if not isinstance(result, dict):
            raise ConnectorError("INCONSISTENT_PAYLOAD", "result is not an object")

        self._protocol_version = str(result.get("protocolVersion") or "2025-06-18")

        notify_payload = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }
        notify_headers = {
            "Authorization": auth_header,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self._protocol_version,
        }
        if self._session_id:
            notify_headers["Mcp-Session-Id"] = self._session_id

        try:
            notify_resp = await client.post(url, json=notify_payload, headers=notify_headers)
        except httpx.HTTPError:
            raise ConnectorError("HOST_ERROR", "request failed") from None

        if notify_resp.status_code not in (200, 202, 204):
            raise ConnectorError("HOST_ERROR", f"status {notify_resp.status_code}")

    async def _call_tool(
        self, client: httpx.AsyncClient, url: str, auth_header: str, spec: McpEntitySpec, page_index: int
    ) -> tuple[List[Dict[str, Any]], bool]:
        req_id = self._next_id()
        args = dict(spec.arguments or {})
        if page_index > 0 and self._next_cursor and spec.cursor_param:
            args[spec.cursor_param] = self._next_cursor

        params: Dict[str, Any] = {"name": spec.tool}
        if spec.arguments is not None or spec.cursor_param is not None:
            params["arguments"] = args

        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": params,
        }
        headers = {
            "Authorization": auth_header,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self._protocol_version,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError:
            raise ConnectorError("HOST_ERROR", "request failed") from None

        if response.status_code != 200:
            raise ConnectorError("HOST_ERROR", f"status {response.status_code}")

        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id

        data = _parse_jsonrpc_response(response, req_id)
        if "error" in data:
            raise ConnectorError("HOST_ERROR")

        result = data.get("result")
        if not isinstance(result, dict):
            raise ConnectorError("INCONSISTENT_PAYLOAD", "result is not an object")
        if result.get("isError") is True:
            raise ConnectorError("HOST_ERROR")

        if "structuredContent" in result and result["structuredContent"] is not None:
            content = result["structuredContent"]
        else:
            raw_content = result.get("content")
            if not isinstance(raw_content, list):
                raise ConnectorError("INCONSISTENT_PAYLOAD", "missing content")
            text_item = next(
                (c for c in raw_content if isinstance(c, dict) and c.get("type") == "text"
                 and isinstance(c.get("text"), str)),
                None,
            )
            if text_item is None:
                raise ConnectorError("INCONSISTENT_PAYLOAD", "no text content")
            try:
                content = json.loads(text_item["text"])
            except Exception:
                raise ConnectorError("INCONSISTENT_PAYLOAD", "not JSON") from None

        if spec.items_at:
            rows = _get(content, spec.items_at)
        else:
            rows = content

        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ConnectorError("INCONSISTENT_PAYLOAD", f"{spec.items_at or 'items'} is not a list of objects")

        if spec.cursor_param and spec.next_cursor_at:
            cursor = _get(content, spec.next_cursor_at) if isinstance(content, dict) else None
            if cursor is None and isinstance(result, dict):
                cursor = _get(result, spec.next_cursor_at)
            if cursor is not None and cursor != "":
                self._next_cursor = str(cursor)
                more = True
            else:
                self._next_cursor = None
                more = False
        else:
            more = False

        return rows, more


class DeclarativeConnector:
    def __init__(
        self,
        host: HostConfig,
        profile: VendorProfileSpec,
        env: Optional[Mapping[str, str]] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        transport: Optional[Union[RestTransport, McpTransport]] = None,
    ):
        if host.vendor != profile.vendor:
            raise ConnectorError("VENDOR_MISMATCH", f"{host.vendor} host with {profile.vendor} profile")
        self._host = host
        self._profile = profile
        self._env = env
        self._http = http_client
        self._transport = transport

    def _auth_header(self) -> str:
        try:
            if self._profile.auth == "frappe_token":
                key, secret = self._host.credentials(self._env)
                return f"token {key}:{secret}"
            return f"Bearer {self._host.bearer_token(self._env)}"
        except HostRegistryError:
            raise ConnectorError("HOST_NOT_CONFIGURED") from None

    def _path(self, spec: AnyEntitySpec) -> str:
        values = {"service_user_id": self._host.service_user_id}

        def fill(match: "re.Match[str]") -> str:
            value = values.get(match.group(1))
            if not value:
                raise ConnectorError("HOST_NOT_CONFIGURED", f"{match.group(1)} is not configured")
            return quote(value, safe="")

        return _TEMPLATE_PARAM.sub(fill, spec.path)

    async def list_entity(self, entity: str) -> ListResult:
        spec = self._profile.entities.get(entity)  # type: ignore[call-overload]
        if spec is None:
            raise ConnectorError("ENTITY_NOT_SUPPORTED", entity)
        auth_header = self._auth_header()
        url = self._host.base_url + quote(self._path(spec), safe="/%")
        own_client = self._http is None
        client = self._http or httpx.AsyncClient(
            timeout=self._host.timeout_seconds, transport=_default_transport()
        )
        transport = self._transport or (
            RestTransport() if self._profile.protocol == "rest" else McpTransport()
        )
        result = ListResult()
        try:
            for page_index in range(MAX_PAGES):
                rows, more = await transport.fetch_page(client, url, auth_header, spec, page_index)  # type: ignore[arg-type]
                result.rows.extend(rows)
                for row in rows:
                    self._convert(row, spec, result)
                if not more:
                    return result
        finally:
            if own_client:
                await client.aclose()
        raise ConnectorError("TOO_MANY_PAGES", f"more than {MAX_PAGES} pages")

    async def _fetch_page(
        self, client: httpx.AsyncClient, url: str, headers: Dict[str, str], spec: EntitySpec, page_index: int
    ) -> "tuple[List[Dict[str, Any]], bool]":
        return await RestTransport().fetch_page(client, url, headers.get("Authorization", ""), spec, page_index)

    @staticmethod
    def _convert(row: Dict[str, Any], spec: AnyEntitySpec, result: ListResult) -> None:
        record_id = _get(row, spec.id_from)
        try:
            if not isinstance(record_id, str) or not record_id:
                raise _Rejected("INVALID_VALUE", "id")
            record: Dict[str, Any] = {"id": record_id}
            for name in REQUISITION_FIELDS:
                fs = spec.fields.get(name)  # type: ignore[call-overload]
                record[name] = None if fs is None else DeclarativeConnector._value(row, name, fs)
        except _Rejected as rejected:
            result.rejected.append({"id": record_id if isinstance(record_id, str) else None,
                                    "error": rejected.error, "field": rejected.field_name})
            return
        result.records.append(record)

    @staticmethod
    def _value(row: Dict[str, Any], name: str, fs: FieldSpec) -> Any:
        raw = _get(row, fs.source)
        if fs.required and (raw is None or raw == ""):
            raise _Rejected("MISSING_VALUE", name)
        if fs.map is not None:
            if not isinstance(raw, str) or raw not in fs.map:
                raise _Rejected("UNMAPPED_VALUE", name)
            return fs.map[raw]
        if fs.type == "int":
            if isinstance(raw, int) and not isinstance(raw, bool):
                value = raw
            elif isinstance(raw, str) and _POSITIVE_INT.fullmatch(raw) and raw.isascii():
                value = int(raw)
            else:
                raise _Rejected("INVALID_VALUE", name)
            if value < 1:
                raise _Rejected("INVALID_VALUE", name)
            return value
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise _Rejected("INVALID_VALUE", name)
        if fs.transform == "strip":
            raw = raw.strip()
        elif fs.transform == "lower":
            raw = raw.lower()
        elif fs.transform == "upper":
            raw = raw.upper()
        if fs.required and raw == "":
            raise _Rejected("MISSING_VALUE", name)
        return raw
