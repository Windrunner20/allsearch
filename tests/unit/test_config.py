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
