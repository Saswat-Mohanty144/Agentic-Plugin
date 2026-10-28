"""Tenant registry validation (spec §6.1)."""

import copy
from pathlib import Path

import pytest
import yaml

from hrms_plugin.security.rbac import UserRole
from hrms_plugin.security.tenant_registry import RegistryError, load_registry, load_registry_from_env

FIXTURE = Path(__file__).parents[1] / "fixtures" / "tenant_registry.yaml"
BASE = yaml.safe_load(FIXTURE.read_text())


def write(tmp_path, data) -> Path:
    path = tmp_path / "reg.yaml"
    path.write_text(yaml.safe_dump(data) if not isinstance(data, str) else data)
    return path


def one(**overrides) -> dict:
    tenant = copy.deepcopy(BASE["tenants"][0])
    tenant.update(overrides)
    return {"tenants": [tenant]}


def test_fixture_loads():
    reg = load_registry(FIXTURE)
    assert [t.tenant_id for t in reg.tenants] == ["acme", "globex", "initech"]
    assert reg.find("https://idp.acme.test", "acme").role_map["hr_manager"] is UserRole.HR_ADMIN


def test_shared_issuer_resolves_each_tenant():
    reg = load_registry(FIXTURE)
    assert reg.find("https://idp.acme.test", "globex").tenant_id == "globex"
    assert reg.claims_for_issuer("https://idp.acme.test").tenant == "tid"


def test_lookups_for_unknown_values_return_none():
    reg = load_registry(FIXTURE)
    assert reg.find("https://idp.acme.test", "initech") is None
    assert reg.find("https://nope.test", "acme") is None
    assert reg.claims_for_issuer("https://nope.test") is None


def test_all_origins_is_sorted_union():
    assert load_registry(FIXTURE).all_origins() == [
        "https://hr.acme.test", "https://hr.globex.test", "https://hr.initech.test:8443"]


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "has space"), ("tenant_id", ""), ("tenant_id", "a" * 65),
    ("issuer", ""), ("audience", ""),
    ("jwks_url", "http://idp.acme.test/jwks.json"), ("jwks_url", "/jwks.json"), ("jwks_url", "https://"),
    ("algorithms", []), ("algorithms", ["HS256"]), ("algorithms", ["none"]),
    ("claims", {"tenant": "", "roles": "roles", "subject": "sub"}),
    ("claims", {"tenant": "tid", "roles": "roles"}),
    ("role_map", {}), ("role_map", {"x": "GOD_MODE"}),
    ("allowed_origins", ["*"]), ("allowed_origins", ["http://hr.acme.test"]),
    ("allowed_origins", ["https://hr.acme.test/"]), ("allowed_origins", ["https://hr.acme.test/app"]),
    ("allowed_origins", ["https://hr.acme.test?x=1"]), ("allowed_origins", ["https://hr.acme.test:99999"]),
    ("leeway_seconds", -1), ("leeway_seconds", 121), ("leeway_seconds", "30"), ("leeway_seconds", True),
    ("max_token_lifetime_seconds", 59), ("max_token_lifetime_seconds", 86401),
])
def test_invalid_field_rejected(tmp_path, field, value):
    with pytest.raises(RegistryError):
        load_registry(write(tmp_path, one(**{field: value})))


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "a" * 64), ("leeway_seconds", 0), ("leeway_seconds", 120),
    ("max_token_lifetime_seconds", 60), ("max_token_lifetime_seconds", 86400),
    ("allowed_origins", []), ("allowed_origins", ["https://hr.acme.test:8443"]),
])
def test_boundary_values_accepted(tmp_path, field, value):
    load_registry(write(tmp_path, one(**{field: value})))


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(RegistryError):
        load_registry(write(tmp_path, one(extra_key=1)))


def test_unknown_top_level_key_rejected(tmp_path):
    data = one()
    data["version"] = 2
    with pytest.raises(RegistryError):
        load_registry(write(tmp_path, data))


def test_empty_tenant_list_rejected(tmp_path):
    with pytest.raises(RegistryError):
        load_registry(write(tmp_path, {"tenants": []}))


def test_duplicate_tenant_id_rejected(tmp_path):
    data = one()
    data["tenants"].append(copy.deepcopy(data["tenants"][0]))
    with pytest.raises(RegistryError, match="duplicate tenant_id"):
        load_registry(write(tmp_path, data))


def test_shared_issuer_with_different_claims_rejected(tmp_path):
    data = copy.deepcopy(BASE)
    data["tenants"][1]["claims"] = {"tenant": "org", "roles": "roles", "subject": "sub"}
    with pytest.raises(RegistryError, match="identical claims"):
        load_registry(write(tmp_path, data))


def test_invalid_yaml_rejected(tmp_path):
    with pytest.raises(RegistryError, match="YAML"):
        load_registry(write(tmp_path, "tenants: [\n"))


def test_non_mapping_document_rejected(tmp_path):
    with pytest.raises(RegistryError):
        load_registry(write(tmp_path, "- just\n- a list\n"))


def test_missing_file_rejected(tmp_path):
    with pytest.raises(RegistryError, match="cannot read"):
        load_registry(tmp_path / "missing.yaml")


def test_env_var_unset_rejected():
    with pytest.raises(RegistryError, match="HRMS_TENANT_REGISTRY"):
        load_registry_from_env({})


def test_env_var_empty_rejected():
    with pytest.raises(RegistryError, match="HRMS_TENANT_REGISTRY"):
        load_registry_from_env({"HRMS_TENANT_REGISTRY": ""})


def test_env_var_loads_file():
    assert load_registry_from_env({"HRMS_TENANT_REGISTRY": str(FIXTURE)}).tenants[0].tenant_id == "acme"


def test_registry_is_immutable():
    reg = load_registry(FIXTURE)
    with pytest.raises(Exception):
        reg.tenants[0].issuer = "x"
