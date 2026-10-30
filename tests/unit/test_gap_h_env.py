"""Tests for Task H follow-up: empty and whitespace environment values behave as unset."""

import pytest

from hrms_plugin.ai.llm import (
    ANTHROPIC_DEFAULT_MODEL,
    LLMGateway,
    LLMNotConfigured,
    LLMProvider,
)
from hrms_plugin.integrations.host_registry import (
    HostConfig,
    HostRegistryError,
    load_host_registry_from_env,
)
from hrms_plugin.payroll.rules import RulesError, load_payroll_rules_from_env
from hrms_plugin.security.tenant_registry import RegistryError, load_registry_from_env
from hrms_plugin.server.app import voice_agent_from_env

_ENV_KEYS = (
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_EFFORT",
    "LLM_PROVIDER_REGION",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_MODEL",
    "NVIDIA_API_KEY",
    "SARVAM_API_KEY",
    "HRMS_HOSTS",
    "HRMS_PAYROLL_RULES",
    "HRMS_TENANT_REGISTRY",
    "HRMS_DATA_DIR",
)


@pytest.fixture
def clean_all_env(monkeypatch):
    """Strip all relevant plugin environment variables before each test."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.mark.parametrize(
    "provider,key_name,default_model",
    [
        ("openai", "OPENAI_API_KEY", "gpt-4o-mini"),
        ("groq", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
        ("openrouter", "OPENROUTER_API_KEY", "meta-llama/llama-3.3-70b-instruct:free"),
        ("anthropic", "ANTHROPIC_API_KEY", ANTHROPIC_DEFAULT_MODEL),
        ("gemini", "GEMINI_API_KEY", "gemini-2.5-flash"),
        ("nvidia", "NVIDIA_API_KEY", "meta/llama-3.1-70b-instruct"),
    ],
)
def test_llm_model_empty_and_whitespace_behaves_as_unset(clean_all_env, provider, key_name, default_model):
    clean_all_env.setenv("LLM_PROVIDER", provider)
    clean_all_env.setenv(key_name, "test-key")

    # Positive case: explicit model override
    clean_all_env.setenv("LLM_MODEL", "custom-model-id")
    assert LLMGateway().config.model == "custom-model-id"

    # Boundary case 1: unset behaves as default
    clean_all_env.delenv("LLM_MODEL", raising=False)
    assert LLMGateway().config.model == default_model

    # Boundary case 2: empty string behaves as unset (default)
    clean_all_env.setenv("LLM_MODEL", "")
    assert LLMGateway().config.model == default_model

    # Boundary case 3: whitespace string behaves as unset (default)
    clean_all_env.setenv("LLM_MODEL", "   ")
    assert LLMGateway().config.model == default_model


def test_llm_provider_empty_and_whitespace_behaves_as_unset(clean_all_env):
    # Empty or whitespace provider with no keys must fail loud
    clean_all_env.setenv("LLM_PROVIDER", "")
    with pytest.raises(LLMNotConfigured, match="no LLM provider configured"):
        LLMGateway()

    clean_all_env.setenv("LLM_PROVIDER", "   ")
    with pytest.raises(LLMNotConfigured, match="no LLM provider configured"):
        LLMGateway()

    # Empty or whitespace provider with API key must autodetect like unset
    clean_all_env.setenv("LLM_PROVIDER", "")
    clean_all_env.setenv("OPENAI_API_KEY", "test-key")
    assert LLMGateway().config.provider == LLMProvider.OPENAI

    clean_all_env.setenv("LLM_PROVIDER", "   ")
    clean_all_env.setenv("ANTHROPIC_API_KEY", "test-key")
    clean_all_env.delenv("OPENAI_API_KEY", raising=False)
    assert LLMGateway().config.provider == LLMProvider.ANTHROPIC

    # Negative case: unknown non-empty provider raises
    clean_all_env.setenv("LLM_PROVIDER", "unknown_provider")
    with pytest.raises(LLMNotConfigured, match="unknown LLM_PROVIDER"):
        LLMGateway()


def test_llm_effort_empty_and_whitespace_behaves_as_unset(clean_all_env):
    clean_all_env.setenv("LLM_PROVIDER", "anthropic")
    clean_all_env.setenv("ANTHROPIC_API_KEY", "test-key")

    # Unset defaults to medium
    clean_all_env.delenv("LLM_EFFORT", raising=False)
    assert LLMGateway().config.effort == "medium"

    # Empty string defaults to medium
    clean_all_env.setenv("LLM_EFFORT", "")
    assert LLMGateway().config.effort == "medium"

    # Whitespace defaults to medium
    clean_all_env.setenv("LLM_EFFORT", "   ")
    assert LLMGateway().config.effort == "medium"

    # Positive case: valid override
    clean_all_env.setenv("LLM_EFFORT", "high")
    assert LLMGateway().config.effort == "high"

    # Negative case: invalid non-empty effort raises
    clean_all_env.setenv("LLM_EFFORT", "extreme")
    with pytest.raises(LLMNotConfigured, match="LLM_EFFORT must be one of"):
        LLMGateway()


def test_llm_provider_region_empty_and_whitespace_behaves_as_unset(clean_all_env):
    clean_all_env.setenv("LLM_PROVIDER", "fake")

    # Unset is None
    clean_all_env.delenv("LLM_PROVIDER_REGION", raising=False)
    assert LLMGateway().provider_region is None

    # Empty string is None
    clean_all_env.setenv("LLM_PROVIDER_REGION", "")
    assert LLMGateway().provider_region is None

    # Whitespace is None
    clean_all_env.setenv("LLM_PROVIDER_REGION", "   ")
    assert LLMGateway().provider_region is None

    # Positive case: region set
    clean_all_env.setenv("LLM_PROVIDER_REGION", "me-central-1")
    assert LLMGateway().provider_region == "me-central-1"


def test_openai_base_url_empty_and_whitespace_behaves_as_unset(clean_all_env):
    clean_all_env.setenv("LLM_PROVIDER", "openai")
    clean_all_env.setenv("OPENAI_API_KEY", "test-key")

    # Unset base_url leaves config None and client default
    clean_all_env.delenv("OPENAI_BASE_URL", raising=False)
    gw = LLMGateway()
    assert gw.config.base_url is None
    assert gw.client.base_url == "https://api.openai.com/v1"

    # Empty string base_url leaves config None and client default
    clean_all_env.setenv("OPENAI_BASE_URL", "")
    gw = LLMGateway()
    assert gw.config.base_url is None
    assert gw.client.base_url == "https://api.openai.com/v1"

    # Whitespace base_url leaves config None and client default
    clean_all_env.setenv("OPENAI_BASE_URL", "   ")
    gw = LLMGateway()
    assert gw.config.base_url is None
    assert gw.client.base_url == "https://api.openai.com/v1"

    # Positive case: custom base URL
    clean_all_env.setenv("OPENAI_BASE_URL", "https://proxy.example.com/v1")
    gw = LLMGateway()
    assert gw.client.base_url == "https://proxy.example.com/v1"


@pytest.mark.parametrize(
    "key_name",
    [
        "OPENAI_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "NVIDIA_API_KEY",
    ],
)
def test_provider_keys_empty_and_whitespace_behave_as_unset(clean_all_env, key_name):
    # Empty key does not autodetect provider
    clean_all_env.setenv(key_name, "")
    with pytest.raises(LLMNotConfigured, match="no LLM provider configured"):
        LLMGateway()

    # Whitespace key does not autodetect provider
    clean_all_env.setenv(key_name, "   ")
    with pytest.raises(LLMNotConfigured, match="no LLM provider configured"):
        LLMGateway()

    # Explicit provider with empty or whitespace key has api_key None
    clean_all_env.setenv("LLM_PROVIDER", "openai")
    clean_all_env.setenv("OPENAI_API_KEY", "")
    assert LLMGateway().config.api_key is None

    clean_all_env.setenv("OPENAI_API_KEY", "   ")
    assert LLMGateway().config.api_key is None


def test_sarvam_api_key_empty_and_whitespace_behaves_as_unset():
    # Unset
    assert voice_agent_from_env({}).api_key is None

    # Empty string
    assert voice_agent_from_env({"SARVAM_API_KEY": ""}).api_key is None

    # Whitespace
    assert voice_agent_from_env({"SARVAM_API_KEY": "   "}).api_key is None

    # Positive case
    assert voice_agent_from_env({"SARVAM_API_KEY": "secret-key"}).api_key == "secret-key"


def test_hrms_hosts_empty_and_whitespace_behaves_as_unset():
    # Unset
    with pytest.raises(HostRegistryError, match="HRMS_HOSTS is not set"):
        load_host_registry_from_env({})

    # Empty string
    with pytest.raises(HostRegistryError, match="HRMS_HOSTS is not set"):
        load_host_registry_from_env({"HRMS_HOSTS": ""})

    # Whitespace
    with pytest.raises(HostRegistryError, match="HRMS_HOSTS is not set"):
        load_host_registry_from_env({"HRMS_HOSTS": "   "})

    # Host credentials env empty and whitespace behave as unset
    host = HostConfig(
        tenant_id="t1",
        vendor="frappe",
        base_url="https://host.local",
        api_key_env="K",
        api_secret_env="S",
        timeout_seconds=10,
    )
    with pytest.raises(HostRegistryError, match="credentials for tenant 't1' are not set"):
        host.credentials({"K": "", "S": ""})

    with pytest.raises(HostRegistryError, match="credentials for tenant 't1' are not set"):
        host.credentials({"K": "   ", "S": "secret"})

    # Host bearer token empty and whitespace behave as unset
    iceipts_host = HostConfig(
        tenant_id="t2",
        vendor="iceipts",
        base_url="https://host.local",
        token_env="TOK",
        service_user_id="12345678-1234-1234-1234-123456789abc",
        timeout_seconds=10,
    )
    with pytest.raises(HostRegistryError, match="token for tenant 't2' is not set"):
        iceipts_host.bearer_token({"TOK": ""})

    with pytest.raises(HostRegistryError, match="token for tenant 't2' is not set"):
        iceipts_host.bearer_token({"TOK": "   "})


def test_hrms_payroll_rules_empty_and_whitespace_behaves_as_unset():
    # Unset
    with pytest.raises(RulesError, match="HRMS_PAYROLL_RULES is not set"):
        load_payroll_rules_from_env({})

    # Empty string
    with pytest.raises(RulesError, match="HRMS_PAYROLL_RULES is not set"):
        load_payroll_rules_from_env({"HRMS_PAYROLL_RULES": ""})

    # Whitespace
    with pytest.raises(RulesError, match="HRMS_PAYROLL_RULES is not set"):
        load_payroll_rules_from_env({"HRMS_PAYROLL_RULES": "   "})


def test_hrms_tenant_registry_empty_and_whitespace_behaves_as_unset():
    # Unset
    with pytest.raises(RegistryError, match="HRMS_TENANT_REGISTRY is not set"):
        load_registry_from_env({})

    # Empty string
    with pytest.raises(RegistryError, match="HRMS_TENANT_REGISTRY is not set"):
        load_registry_from_env({"HRMS_TENANT_REGISTRY": ""})

    # Whitespace
    with pytest.raises(RegistryError, match="HRMS_TENANT_REGISTRY is not set"):
        load_registry_from_env({"HRMS_TENANT_REGISTRY": "   "})


def test_hrms_data_dir_empty_and_whitespace_behaves_as_unset(clean_all_env):
    clean_all_env.setenv("LLM_PROVIDER", "fake")

    # When unset, defaults to .hrms_data
    clean_all_env.delenv("HRMS_DATA_DIR", raising=False)
    gw1 = LLMGateway()
    assert ".hrms_data" in str(gw1.egress_log._store.db_path)

    # When empty string, must also default to .hrms_data, not empty relative path
    clean_all_env.setenv("HRMS_DATA_DIR", "")
    gw2 = LLMGateway()
    assert ".hrms_data" in str(gw2.egress_log._store.db_path)

    # When whitespace, must also default to .hrms_data
    clean_all_env.setenv("HRMS_DATA_DIR", "   ")
    gw3 = LLMGateway()
    assert ".hrms_data" in str(gw3.egress_log._store.db_path)
