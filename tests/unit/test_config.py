from __future__ import annotations

import pytest

from allsearch.config import load_config
from allsearch.errors import ConfigError


def test_process_env_wins_over_dotenv(tmp_path, clean_env):
    env_file = tmp_path / ".env"
    env_file.write_text("ALLSEARCH_XAI_MODEL=from-dotenv\nALLSEARCH_SERVER_NAME=FromDotEnv\n")
    clean_env.setenv("ALLSEARCH_XAI_MODEL", "from-process")
    cfg = load_config(env_file=env_file)
    assert cfg.xai.model == "from-process"


def test_missing_keys_do_not_crash_config(clean_env):
    # Isolate from a developer's gitignored project .env.
    cfg = load_config(env_file="/dev/null")
    assert cfg.xai.api_key is None
    assert cfg.tavily.api_key is None
    assert not cfg.xai.configured()
    # repr redacts secrets even when present
    clean_env.setenv("ALLSEARCH_XAI_API_KEY", "super-secret-key-value")
    cfg2 = load_config()
    assert "super-secret" not in repr(cfg2)
    assert cfg2.public_dict()["xai"]["api_key"] is True


def test_malformed_int_raises(clean_env):
    clean_env.setenv("ALLSEARCH_MCP_PORT", "nope")
    with pytest.raises(ConfigError):
        load_config()


def test_degraded_default_false(clean_env):
    cfg = load_config(env_file="/dev/null")
    assert cfg.allow_degraded_search is False


def test_gateway_model_defaults(clean_env):
    cfg = load_config(env_file="/dev/null")
    assert cfg.xai.model == "grok-4.5"
    assert cfg.xai.fallback_models == ("grok-4.3",)
    assert cfg.xai.reasoning_effort == "low"
    assert cfg.xai.max_tool_calls == 4


def test_public_dict_redacts_fallback_and_pool(clean_env):
    clean_env.setenv("ALLSEARCH_XAI_API_KEY", "xai-secret-1")
    clean_env.setenv("ALLSEARCH_TAVILY_API_KEY", "tvly-secret-1")
    clean_env.setenv("ALLSEARCH_TAVILY_API_KEYS", "tvly-secret-2,tvly-secret-3")
    clean_env.setenv("ALLSEARCH_XAI_FALLBACK_BASE_URL", "https://fallback.example/v1")
    clean_env.setenv("ALLSEARCH_XAI_FALLBACK_API_KEY", "fb-secret-1")
    cfg = load_config(env_file="/dev/null")
    pub = cfg.public_dict()
    # Nested fallback api_key redacted to a boolean.
    assert pub["xai"]["api_key"] is True
    assert pub["xai"]["fallback_endpoint"]["api_key"] is True
    # extra_api_keys exposed only as a count, never the raw values.
    assert pub["tavily"]["extra_api_keys"] == 2
    blob = str(pub)
    assert "fb-secret-1" not in blob
    assert "tvly-secret-1" not in blob
    assert "tvly-secret-2" not in blob
    assert "tvly-secret-3" not in blob


def test_fallback_endpoint_repr_redacts_api_key(clean_env):
    clean_env.setenv("ALLSEARCH_XAI_FALLBACK_BASE_URL", "https://fallback.example/v1")
    clean_env.setenv("ALLSEARCH_XAI_FALLBACK_API_KEY", "super-secret-fb")
    cfg = load_config(env_file="/dev/null")
    assert "super-secret-fb" not in repr(cfg.xai.fallback_endpoint)
    assert "super-secret-fb" not in repr(cfg)


def test_fallback_protocol_responses_rejected(clean_env):
    clean_env.setenv("ALLSEARCH_XAI_FALLBACK_BASE_URL", "https://fallback.example/v1")
    clean_env.setenv("ALLSEARCH_XAI_FALLBACK_API_KEY", "k")
    clean_env.setenv("ALLSEARCH_XAI_FALLBACK_PROTOCOL", "responses")
    with pytest.raises(ConfigError, match="openai"):
        load_config()


def test_fallback_protocol_default_openai(clean_env):
    clean_env.setenv("ALLSEARCH_XAI_FALLBACK_BASE_URL", "https://fallback.example/v1")
    clean_env.setenv("ALLSEARCH_XAI_FALLBACK_API_KEY", "k")
    cfg = load_config(env_file="/dev/null")
    assert cfg.xai.fallback_endpoint is not None
    assert cfg.xai.fallback_endpoint.protocol == "openai"


def test_tavily_pool_only_configured(clean_env):
    clean_env.setenv("ALLSEARCH_TAVILY_API_KEYS", "k2,k3")
    cfg = load_config(env_file="/dev/null")
    assert cfg.tavily.api_key is None
    assert cfg.tavily.configured() is True
    assert cfg.tavily.all_keys() == ("k2", "k3")


def test_load_config_parses_tavily_pool_and_dedupes(clean_env):
    clean_env.setenv("ALLSEARCH_TAVILY_API_KEY", "primary")
    clean_env.setenv("ALLSEARCH_TAVILY_API_KEYS", 'k2, primary, "k3"\n k4')
    cfg = load_config(env_file="/dev/null")
    assert cfg.tavily.extra_api_keys == ("k2", "k3", "k4")
    assert cfg.tavily.all_keys() == ("primary", "k2", "k3", "k4")
