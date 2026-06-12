"""Entrypoint: launch the MCP server over stdio (default) or hardened HTTP.

stdio is the default and most secure mode: the server runs as a local subprocess
of the agent with no network exposure (NSA: design for boundaries / prefer local).
The optional ``--transport http`` mode is hardened — it refuses to start without a
bearer token and binds to localhost by default.
"""

from __future__ import annotations

import argparse
import hmac
import sys

from mcp.server.fastmcp import FastMCP

from .config import Settings, load_settings
from .server import build_server


def _run_http(mcp: FastMCP, settings: Settings) -> None:
    """Run the streamable-HTTP transport behind mandatory bearer-token auth."""
    if not settings.http_auth_token:
        sys.exit(
            "Refusing to start HTTP transport without PERPLEXITY_HTTP_AUTH_TOKEN set. "
            "Set a strong token, or use the default stdio transport."
        )

    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.types import ASGIApp, Receive, Scope, Send

    expected = b"Bearer " + settings.http_auth_token.get_secret_value().encode()

    class BearerAuthMiddleware:
        """Reject any request without the exact expected bearer token."""

        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"")
            # Constant-time compare: a plain `!=` short-circuits on the first
            # differing byte, leaking the token's length/prefix via timing. Compare
            # raw bytes so a non-ASCII header can't raise instead of returning 401.
            if not hmac.compare_digest(provided, expected):
                resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                await resp(scope, receive, send)
                return
            await self.app(scope, receive, send)

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware)
    uvicorn.run(app, host=settings.http_host, port=settings.http_port)


def _run_tui(settings: Settings) -> None:
    """Launch the interactive Comet-style TUI (needs the optional ``tui`` extra)."""
    try:
        from .tui import run_tui
    except ImportError as exc:
        sys.exit(
            "The interactive TUI needs the optional 'tui' extra. Install it with:\n"
            "    uv sync --extra tui   (or: pip install 'perplexity-agent[tui]')\n"
            f"Underlying import error: {exc}"
        )
    run_tui(settings)


def main() -> None:
    parser = argparse.ArgumentParser(prog="perplexity-agent", description=__doc__)
    # Backward-compatible: `perplexity-agent` and `perplexity-agent --transport ...`
    # still run the MCP server. A `tui` subcommand launches the interactive UI.
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=None,  # sentinel: distinguishes "not passed" from an explicit "stdio"
        help="MCP transport (default: stdio). 'http' requires PERPLEXITY_HTTP_AUTH_TOKEN.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("tui", help="Launch the interactive Comet-style terminal UI.")
    args = parser.parse_args()

    if args.command == "tui":
        # --transport only governs the MCP server; pairing it with `tui` is a
        # mistake (e.g. a headless unit edited to add `tui`) and must not silently
        # discard the flag and launch an interactive UI instead.
        if args.transport is not None:
            parser.error("--transport is not valid with the 'tui' subcommand.")
        # Loaded after the arg check so a bad invocation errors regardless of env.
        settings = load_settings()
        _run_tui(settings)
        return

    settings = load_settings()  # fail fast if the API key is missing
    mcp, settings = build_server(settings)
    if args.transport == "http":
        _run_http(mcp, settings)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
