import pytest
from pydantic import SecretStr

from perplexity_agent.config import Settings


def _settings(**kw):
    return Settings(api_key=SecretStr("pplx-testkey1234567890"), **kw)


def test_retention_defaults_to_unset():
    s = _settings()
    assert s.max_history_per_space is None
    assert s.max_tabs_per_space is None


def test_blank_retention_env_is_treated_as_unset(monkeypatch):
    # A blank `PERPLEXITY_MAX_HISTORY_PER_SPACE=` (common when copying .env.example)
    # must NOT crash startup — it means "keep everything".
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-testkey1234567890")
    monkeypatch.setenv("PERPLEXITY_MAX_HISTORY_PER_SPACE", "")
    monkeypatch.setenv("PERPLEXITY_MAX_TABS_PER_SPACE", "  ")
    s = Settings()  # type: ignore[call-arg]
    assert s.max_history_per_space is None
    assert s.max_tabs_per_space is None


def test_retention_rejects_non_positive():
    with pytest.raises(ValueError):
        _settings(max_history_per_space=0)
