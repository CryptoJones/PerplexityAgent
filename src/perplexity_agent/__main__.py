"""Entrypoint: launch the MCP server over stdio (default) or hardened HTTP.

stdio is the default and most secure mode: the server runs as a local subprocess
of the agent with no network exposure (NSA: design for boundaries / prefer local).
The optional ``--transport http`` mode is hardened — it refuses to start without a
bearer token and binds to localhost by default.
"""

from __future__ import annotations

import argparse
import sys

from .config import load_settings
from .server import build_server


def _run_http(mcp, settings) -> None:
    """Run the streamable-HTTP transport behind mandatory bearer-token auth."""
    if not settings.http_auth_token:
        sys.exit(
            "Refusing to start HTTP transport without PERPLEXITY_HTTP_AUTH_TOKEN set. "
            "Set a strong token, or use the default stdio transport."
        )

    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.types import ASGIApp, Receive, Scope, Send

    expected = f"Bearer {settings.http_auth_token.get_secret_value()}"

    class BearerAuthMiddleware:
        """Reject any request without the exact expected bearer token."""

        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"").decode()
            if provided != expected:
                resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                await resp(scope, receive, send)
                return
            await self.app(scope, receive, send)

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware)
    uvicorn.run(app, host=settings.http_host, port=settings.http_port)


def main() -> None:
    parser = argparse.ArgumentParser(prog="perplexity-agent", description=__doc__)
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport (default: stdio). 'http' requires PERPLEXITY_HTTP_AUTH_TOKEN.",
    )
    args = parser.parse_args()

    settings = load_settings()  # fail fast if the API key is missing
    mcp, settings = build_server(settings)

    if args.transport == "http":
        _run_http(mcp, settings)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
