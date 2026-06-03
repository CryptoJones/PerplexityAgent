import pytest
from pydantic import SecretStr

from perplexity_agent.config import Settings


@pytest.fixture
def settings() -> Settings:
    """A Settings instance with a dummy key and fast, deterministic limits."""
    return Settings(
        api_key=SecretStr("pplx-testkey1234567890"),
        max_retries=0,
        rate_per_minute=600,
        rate_burst=100,
    )
