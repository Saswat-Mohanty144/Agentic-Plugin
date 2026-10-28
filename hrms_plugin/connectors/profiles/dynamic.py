"""Dynamic Vendor Profile adapted at runtime from OpenAPI/Swagger or custom route mappings."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from hrms_plugin.connectors.profiles.base_profile import VendorProfile
from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import IntrospectionResult


class DynamicVendorProfile(VendorProfile):
    """Host profile that dynamically routes to endpoints discovered during schema introspection."""

    vendor_name: str = "dynamic"

    def __init__(
        self,
        vendor_name: str = "dynamic",
        endpoint_map: Optional[Dict[Tuple[str, str], str]] = None,
        data_wrapper_keys: Optional[Tuple[str, ...]] = None,
        auth_header_format: str = "Bearer {token}",
        auth_header_name: str = "Authorization",
    ) -> None:
        self.vendor_name = vendor_name
        # structure: {(entity_type_str, action_str): url_template}
        self.endpoint_map: Dict[Tuple[str, str], str] = dict(endpoint_map or {})
        self.data_wrapper_keys = data_wrapper_keys or ("data", "items", "records", "result", "rows")
        self.auth_header_format = auth_header_format
        self.auth_header_name = auth_header_name

    @classmethod
    def _normalize_keys(cls, name: str) -> list[str]:
        keys = {name.upper(), re.sub(r"[^A-Z0-9]", "", name.upper())}
        # camelCase / PascalCase to SNAKE_CASE
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()
        keys.add(snake)
        # Match against known EntityType enum values
        for et in EntityType:
            clean_et = re.sub(r"[^A-Z0-9]", "", et.value)
            clean_name = re.sub(r"[^A-Z0-9]", "", name.upper())
            if clean_et == clean_name or clean_et == clean_name.rstrip("S"):
                keys.add(et.value)
        return list(keys)

    @classmethod
    def from_introspection(
        cls,
        result: IntrospectionResult,
        vendor_name: str = "dynamic",
    ) -> DynamicVendorProfile:
        """Construct a DynamicVendorProfile by reading discovered endpoints from an IntrospectionResult."""
        endpoint_map: Dict[Tuple[str, str], str] = {}

        for ent_name, ent_obj in result.entities.items():
            ent_keys = cls._normalize_keys(ent_name)

            # Map read endpoint
            if ent_obj.read_endpoint:
                parts = ent_obj.read_endpoint.split(" ", 1)
                url = parts[1] if len(parts) > 1 else parts[0]
                if "{" in url:
                    for k in ent_keys:
                        endpoint_map[(k, "get")] = url
                    # derive list path by stripping trailing id param
                    list_url = re.sub(r"/\{[^}]+\}$", "", url)
                    for k in ent_keys:
                        endpoint_map[(k, "list")] = list_url
                else:
                    for k in ent_keys:
                        endpoint_map[(k, "list")] = url
                        endpoint_map[(k, "get")] = f"{url}/{{id}}"

            # Map write endpoint
            if ent_obj.write_endpoint:
                parts = ent_obj.write_endpoint.split(" ", 1)
                url = parts[1] if len(parts) > 1 else parts[0]
                for k in ent_keys:
                    endpoint_map[(k, "create")] = url
                    endpoint_map[(k, "update")] = url if "{" in url else f"{url}/{{id}}"

        return cls(vendor_name=vendor_name, endpoint_map=endpoint_map)

    def register_endpoint(self, entity_type: str | EntityType, action: str, path_template: str) -> None:
        """Register or override a custom endpoint route."""
        ent_key = entity_type.value if isinstance(entity_type, EntityType) else str(entity_type).upper()
        self.endpoint_map[(ent_key, action.lower())] = path_template

    def endpoint_for(
        self,
        entity_type: EntityType,
        action: str,
        entity_id: Optional[str] = None,
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        ent_key = entity_type.value if isinstance(entity_type, EntityType) else str(entity_type).upper()
        act = action.lower()

        # 1. Check exact (entity, action) in endpoint map
        template = self.endpoint_map.get((ent_key, act))

        # 2. Check fallback for action synonyms (e.g. "fetch" -> "get")
        if not template and act in ("fetch", "read"):
            template = self.endpoint_map.get((ent_key, "get"))
        if not template and act in ("post", "add"):
            template = self.endpoint_map.get((ent_key, "create"))

        # 3. Default REST convention if not in map
        if not template:
            plural = ent_key.lower().replace("_", "-") + "s"
            if act in ("get", "fetch", "update", "patch", "delete"):
                template = f"/{plural}/{{id}}"
            else:
                template = f"/{plural}"

        # Interpolate entity_id and kwargs
        path = template
        if entity_id:
            path = path.replace("{id}", str(entity_id))
            path = path.replace(f"{{{ent_key.lower()}_id}}", str(entity_id))
            path = path.replace(f"{{{ent_key.lower()}Id}}", str(entity_id))

        for k, v in kwargs.items():
            path = path.replace(f"{{{k}}}", str(v))

        return path

    def prepare_headers(
        self,
        auth_token: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if auth_token:
            val = self.auth_header_format.format(token=auth_token)
            headers[self.auth_header_name] = val
        if custom_headers:
            headers.update(custom_headers)
        return headers

    def unwrap_response(
        self,
        response_data: Any,
        action: str,
        entity_type: EntityType,
    ) -> Any:
        if isinstance(response_data, dict):
            for k in self.data_wrapper_keys:
                if k in response_data:
                    inner = response_data[k]
                    if isinstance(inner, dict) and "items" in inner:
                        return inner["items"]
                    return inner
        return response_data

    def normalize_payload_for_host(
        self,
        canonical_payload: Dict[str, Any],
        entity_type: EntityType,
        action: str,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        return canonical_payload
