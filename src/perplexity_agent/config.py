"""Configuration and settings.

The Perplexity API key is loaded **server-side only** from the environment (or a
local ``.env``) and is never exposed through any MCP tool output. The server
fails fast at startup if the key is absent (NSA: access control / token security).
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, populated from environment variables / ``.env``.

    With ``env_prefix="PERPLEXITY_"`` each field reads from its upper-cased,
    prefixed env var (e.g. ``api_key`` <- ``PERPLEXITY_API_KEY``).
    """

    model_config = SettingsConfigDict(
        env_prefix="PERPLEXITY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Credentials (required) ---
    # SecretStr keeps the key out of __repr__/logs by default.
    api_key: SecretStr = Field(...)

    # --- API endpoint ---
    base_url: str = "https://api.perplexity.ai"

    # --- Transport / robustness limits (NSA: constrain & sandbox, DoS guard) ---
    timeout: float = Field(default=60.0, gt=0, le=300)
    max_response_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    max_retries: int = Field(default=3, ge=0, le=8)

    # --- Rate limiting (token bucket; NSA: DoS / fatigue) ---
    rate_per_minute: float = Field(default=60.0, gt=0)
    rate_burst: int = Field(default=10, gt=0)

    # --- Audit logging (NSA: instrument for logging & detection) ---
    audit_log_path: str | None = None

    # --- Optional hardened HTTP transport (off by default) ---
    http_auth_token: SecretStr | None = None
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8080, gt=0, le=65535)


def load_settings() -> Settings:
    """Load and validate settings, raising a clear error if the key is missing."""
    try:
        return Settings()  # type: ignore[call-arg]  # values come from the environment
    except Exception as exc:  # pragma: no cover - exercised via integration/startup
        raise RuntimeError(
            "Failed to load configuration. Ensure PERPLEXITY_API_KEY is set "
            "(in the environment or a local .env file). "
            f"Underlying error: {exc}"
        ) from exc
