"""Tests for Task H (decisions D10, D11): deploy files and CI triggers."""

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

ROOT = Path(__file__).parents[2]
DEPLOY = ROOT / "deploy"
REMOVED_ENV_VARS = {
    "ENVIRONMENT",
    "LOG_LEVEL",
    "DEFAULT_JURISDICTION",
    "HOST_HRMS_BASE_URL",
    "HRMS_VENDOR_PROFILE",
    "JWT_SECRET",
}
OPTIONAL_PASS_THROUGH_VARS = [
    "LLM_PROVIDER",
    "LLM_MODEL",
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "NVIDIA_API_KEY",
    "SARVAM_API_KEY",
    "HRMS_HOSTS",
    "HRMS_PAYROLL_RULES",
    "HRMS_AUDIT_SIGNING_KEY_FILE",
    "HRMS_AUDIT_KEY_ID",
]


def _load_compose() -> Dict[str, Any]:
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())


def _load_k8s_docs(filename: str) -> List[Dict[str, Any]]:
    path = DEPLOY / "k8s" / filename
    if not path.exists():
        return []
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _load_ci() -> Dict[str, Any]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())


def _parse_env_list_or_dict(env_block: Any) -> Dict[str, Any]:
    if isinstance(env_block, dict):
        return {str(k): str(v) for k, v in env_block.items()}
    result = {}
    for item in env_block or []:
        if isinstance(item, str):
            if "=" in item:
                k, v = item.split("=", 1)
                result[k.strip()] = v.strip()
            else:
                result[item.strip()] = None
        elif isinstance(item, dict) and "name" in item:
            result[item["name"]] = item.get("value", "")
    return result


def test_compose_volume_and_mount():
    compose = _load_compose()
    api = compose["services"]["hrms-plugin-api"]
    volumes = api.get("volumes", [])
    # Must mount named volume hrms-data to /var/lib/hrms
    assert any("hrms-data:/var/lib/hrms" in str(v) for v in volumes), "missing hrms-data mount at /var/lib/hrms"
    # Top-level named volume hrms-data must be declared
    assert "volumes" in compose and "hrms-data" in compose["volumes"], "top-level volume hrms-data missing"
    # Tenant registry mount must be preserved and read-only
    registry_mount = next((v for v in volumes if "/etc/hrms/tenants.yaml" in str(v)), None)
    assert registry_mount is not None, "tenant registry mount was removed"
    assert "ro" in str(registry_mount)


def test_compose_removed_env_vars_absent():
    compose = _load_compose()
    api = compose["services"]["hrms-plugin-api"]
    env_vars = _parse_env_list_or_dict(api.get("environment"))
    present_removed = set(env_vars.keys()) & REMOVED_ENV_VARS
    assert not present_removed, f"removed env vars still present in compose: {present_removed}"


def test_compose_optional_pass_through_env_vars():
    compose = _load_compose()
    api = compose["services"]["hrms-plugin-api"]
    raw_env = api.get("environment", [])
    env_vars = _parse_env_list_or_dict(raw_env)
    # Kept required variables
    assert env_vars.get("PORT") == "8000"
    assert env_vars.get("HRMS_TENANT_REGISTRY") == "/etc/hrms/tenants.yaml"
    assert env_vars.get("HRMS_DATA_DIR") == "/var/lib/hrms"
    # Compose entries must have no "=${" form
    assert not any("=${" in str(item) for item in raw_env), "compose entries must have no '=${' form"
    # Pass-through variables must be bare entries without a value (- VAR)
    for var in OPTIONAL_PASS_THROUGH_VARS:
        assert var in raw_env, f"missing bare pass-through variable {var} in compose"
        assert not any(str(item).startswith(f"{var}=") for item in raw_env), (
            f"pass-through var {var} must not have value assignment in compose"
        )


def test_k8s_pvc_definition_exists():
    all_k8s = _load_k8s_docs("pvc.yaml") + _load_k8s_docs("deployment.yaml")
    pvc = next((doc for doc in all_k8s if doc.get("kind") == "PersistentVolumeClaim"), None)
    assert pvc is not None, "PersistentVolumeClaim resource not found in deploy/k8s/*.yaml"
    spec = pvc.get("spec", {})
    assert "ReadWriteOnce" in spec.get("accessModes", []), "PVC accessModes must include ReadWriteOnce"
    assert spec.get("resources", {}).get("requests", {}).get("storage") == "1Gi", "PVC request must be 1Gi"


def test_k8s_deployment_pvc_and_tmp_mounted():
    docs = _load_k8s_docs("deployment.yaml")
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]
    volumes = pod_spec.get("volumes", [])
    container = pod_spec["containers"][0]
    volume_mounts = container.get("volumeMounts", [])

    # PVC volume attached to pod
    pvc_vol = next((v for v in volumes if "persistentVolumeClaim" in v), None)
    assert pvc_vol is not None, "no persistentVolumeClaim volume in pod spec"
    claim_name = pvc_vol["persistentVolumeClaim"].get("claimName")
    assert claim_name, "missing claimName on persistentVolumeClaim"

    # PVC mounted at /var/lib/hrms
    pvc_mount = next((m for m in volume_mounts if m.get("mountPath") == "/var/lib/hrms"), None)
    assert pvc_mount is not None, "missing volume mount at /var/lib/hrms"
    assert pvc_mount["name"] == pvc_vol["name"], "mount name must match PVC volume name"

    # emptyDir volume at /tmp
    tmp_vol = next((v for v in volumes if "emptyDir" in v), None)
    assert tmp_vol is not None, "missing emptyDir volume for /tmp"
    tmp_mount = next((m for m in volume_mounts if m.get("mountPath") == "/tmp"), None)
    assert tmp_mount is not None, "missing volume mount at /tmp"
    assert tmp_mount["name"] == tmp_vol["name"], "tmp mount name must match emptyDir volume name"


def test_k8s_replicas_and_recreate_strategy():
    text = (DEPLOY / "k8s" / "deployment.yaml").read_text()
    docs = _load_k8s_docs("deployment.yaml")
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    spec = deployment["spec"]

    assert spec.get("replicas") == 1, f"replicas must be 1 for SQLite single-writer, got {spec.get('replicas')}"
    assert spec.get("strategy", {}).get("type") == "Recreate", "deployment strategy must be Recreate"
    # Comment explaining single-writer SQLite constraint
    assert "single-writer" in text.lower(), "missing comment noting SQLite single-writer in deployment.yaml"
    assert "shared store" in text.lower(), "missing comment noting shared store requirement in deployment.yaml"


def test_k8s_security_context_pod_and_container():
    docs = _load_k8s_docs("deployment.yaml")
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]

    pod_sec = pod_spec.get("securityContext", {})
    assert pod_sec.get("runAsNonRoot") is True, "pod runAsNonRoot must be true"
    assert pod_sec.get("runAsUser") == 10001, "pod runAsUser must be 10001"
    assert pod_sec.get("runAsGroup") == 10001, "pod runAsGroup must be 10001"
    assert pod_sec.get("fsGroup") == 10001, "pod fsGroup must be 10001"

    cont_sec = container.get("securityContext", {})
    assert cont_sec.get("allowPrivilegeEscalation") is False, "container allowPrivilegeEscalation must be false"
    assert cont_sec.get("readOnlyRootFilesystem") is True, "container readOnlyRootFilesystem must be true"
    capabilities = cont_sec.get("capabilities", {})
    assert "ALL" in capabilities.get("drop", []), "container capabilities must drop ALL"


def test_k8s_env_and_secrets():
    docs = _load_k8s_docs("deployment.yaml")
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    # envFrom hrms-plugin-secrets optional: true
    env_from = container.get("envFrom", [])
    secret_ref = next((ef.get("secretRef") for ef in env_from if "secretRef" in ef), None)
    assert secret_ref is not None, "missing envFrom secretRef"
    assert secret_ref.get("name") == "hrms-plugin-secrets"
    assert secret_ref.get("optional") is True

    # Removed env vars absent from container env
    env_dict = _parse_env_list_or_dict(container.get("env"))
    present_removed = set(env_dict.keys()) & REMOVED_ENV_VARS
    assert not present_removed, f"removed env vars still present in k8s container env: {present_removed}"

    # Probes and resources preserved
    assert container.get("readinessProbe") is not None, "readinessProbe must be preserved"
    assert container.get("livenessProbe") is not None, "livenessProbe must be preserved"
    assert container.get("resources") is not None, "resources must be preserved"


def test_ci_triggers_cover_all_branches():
    ci = _load_ci()
    on_block = ci.get("on") or ci.get(True) or {}
    assert "push" in on_block, "push trigger missing in ci.yml"
    assert "pull_request" in on_block, "pull_request trigger missing in ci.yml"

    push_branches = on_block["push"].get("branches", [])
    assert "**" in push_branches or push_branches == ["**"], f"push branches must include '**', got {push_branches}"

    pr_branches = on_block["pull_request"].get("branches")
    assert pr_branches is None or "**" in pr_branches, f"PR branches must cover all branches, got {pr_branches}"


# --- Boundary and negative case tests ---


def test_boundary_removed_var_detection():
    # Verify our detection flags any removed variable
    mock_env = ["PORT=8000", "ENVIRONMENT=production", "HRMS_TENANT_REGISTRY=/etc/hrms/tenants.yaml"]
    parsed = _parse_env_list_or_dict(mock_env)
    assert set(parsed.keys()) & REMOVED_ENV_VARS == {"ENVIRONMENT"}


def test_boundary_pass_through_syntax_validation():
    # Pass-through variables with ${VAR:-} or = form are rejected
    bad_items = ["LLM_PROVIDER=${LLM_PROVIDER:-}", "LLM_MODEL=", "OPENAI_API_KEY=${OPENAI_API_KEY}"]
    for item in bad_items:
        with pytest.raises(AssertionError):
            assert "=${" not in item, "compose entries must have no '=${' form"
            assert "=" not in item, "pass-through var must not have value assignment in compose"


def test_boundary_unsafe_pod_security_rejected():
    # Pod with non-root false or root UID (0) fails
    unsafe_pod_sec = {"runAsNonRoot": False, "runAsUser": 0}
    with pytest.raises(AssertionError):
        assert unsafe_pod_sec.get("runAsNonRoot") is True
    with pytest.raises(AssertionError):
        assert unsafe_pod_sec.get("runAsUser") == 10001


def test_boundary_unsafe_container_security_rejected():
    # Container with allowPrivilegeEscalation or missing capability drop fails
    unsafe_container_sec = {"allowPrivilegeEscalation": True, "readOnlyRootFilesystem": True, "capabilities": {}}
    with pytest.raises(AssertionError):
        assert unsafe_container_sec.get("allowPrivilegeEscalation") is False
    with pytest.raises(AssertionError):
        assert "ALL" in unsafe_container_sec.get("capabilities", {}).get("drop", [])


def test_boundary_ci_main_only_trigger_rejected():
    # A workflow restricted to main fails the all-branches requirement
    main_only_on = {"push": {"branches": ["main"]}, "pull_request": {"branches": ["main"]}}
    push_branches = main_only_on["push"].get("branches", [])
    assert not ("**" in push_branches or push_branches == ["**"])
