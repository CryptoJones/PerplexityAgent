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

    # --- Output efficiency (token budgeting; context-flood guard) ---
    # Per-result char budget handed to the model. A result over this is bounded
    # and the full value offloaded behind a retrieve_key (see efficiency.py).
    max_tool_output_chars: int = Field(default=100_000, gt=0)

    # --- Rate limiting (token bucket; NSA: DoS / fatigue) ---
    rate_per_minute: float = Field(default=60.0, gt=0)
    rate_burst: int = Field(default=10, gt=0)

    # --- Audit logging (NSA: instrument for logging & detection) ---
    audit_log_path: str | None = None

    # --- Optional hardened HTTP transport (off by default) ---
    http_auth_token: SecretStr | None = None
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8080, gt=0, le=65535)

    # --- Interactive TUI: page fetcher + local store (used only by `tui`) ---
    # The fetcher is the only egress path other than api.perplexity.ai. It is
    # reachable solely from the interactive TUI, never via the MCP tools. SSRF
    # controls live in fetch.py; these knobs tune them. Private/loopback/link-local
    # targets are denied by default (fetch_allow_private=False).
    fetch_user_agent: str = "PerplexityAgent-TUI/0.2 (+https://codeberg.org/CryptoJones/PerplexityAgent)"
    fetch_allow_private: bool = False
    # Where the TUI keeps its sqlite store (history, tabs, spaces, facts). None ->
    # an XDG-style default under the user's data dir, resolved in memory.py.
    store_path: str | None = None


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
