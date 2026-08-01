"""Entrypoint: launch the MCP server over stdio (default) or hardened HTTP.

stdio is the default and most secure mode: the server runs as a local subprocess
of the agent with no network exposure (NSA: design for boundaries / prefer local).
The optional ``--transport http`` mode is hardened — it refuses to start without a
bearer token and binds to localhost by default.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import hmac
import logging
import signal
import sys

import anyio
import mcp.types as mcp_types
from mcp.server.mcpserver import MCPServer
from mcp.shared.message import SessionMessage

from .config import Settings, load_settings
from .server import build_server

# Max bytes for a single JSON-RPC line on stdin. asyncio.StreamReader defaults to
# a 64 KiB line limit and raises on anything larger, which would crash the whole
# transport task group on an over-64K request — a legitimately large tool call, or
# a hostile oversized one. Raise it to a generous-but-bounded cap so real traffic
# passes and a runaway line still can't exhaust memory.
_MAX_STDIN_LINE = 16 * 1024 * 1024  # 16 MiB


async def _serve_stdio(mcp: MCPServer) -> None:
    """Serve stdio and close the output stream when the client input reaches EOF.

    MCP 1.28's stock ``stdio_server`` can wait forever on its transport pumps after
    EOF. This small transport owns those pumps and cancels the group as soon as
    either stdin or the server ends, so a detached client cannot leave a deaf
    process running indefinitely.
    """
    read_sender, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_receiver = anyio.create_memory_object_stream[SessionMessage](0)
    finished = anyio.Event()

    async def stdin_reader() -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=_MAX_STDIN_LINE)
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
        try:
            async with read_sender:
                while True:
                    try:
                        line = await reader.readline()
                    except ValueError as exc:
                        # A single line exceeded _MAX_STDIN_LINE. The overrun data
                        # stays buffered and would re-raise on every retry, so we
                        # surface one protocol error and stop reading — a clean end
                        # to the transport, never an unhandled crash of the whole
                        # task group (the previous behaviour). 16 MiB is far above
                        # any legitimate JSON-RPC line, so this is the hostile case.
                        await read_sender.send(exc)
                        break
                    if not line:
                        break
                    try:
                        message = mcp_types.jsonrpc_message_adapter.validate_json(
                            line, by_name=False
                        )
                    except Exception as exc:
                        await read_sender.send(exc)
                        continue
                    await read_sender.send(SessionMessage(message))
        finally:
            transport.close()
            finished.set()

    async def stdout_writer() -> None:
        async with write_receiver:
            async for session_message in write_receiver:
                rendered = session_message.message.model_dump_json(
                    by_alias=True, exclude_unset=True
                )
                sys.stdout.write(rendered + "\n")
                sys.stdout.flush()
                await anyio.lowlevel.checkpoint()

    async def server_runner() -> None:
        try:
            await mcp._lowlevel_server.run(
                read_stream,
                write_stream,
                mcp._lowlevel_server.create_initialization_options(),
            )
        finally:
            finished.set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(stdin_reader)
        task_group.start_soon(stdout_writer)
        task_group.start_soon(server_runner)
        await finished.wait()
        await write_stream.aclose()
        task_group.cancel_scope.cancel()


def _run_stdio(mcp: MCPServer) -> None:
    anyio.run(_serve_stdio, mcp)


def _run_http(mcp: MCPServer, settings: Settings) -> None:
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


def _install_graceful_shutdown() -> None:
    """Flush audit-log handlers on normal exit and on SIGTERM before terminating.

    The running transports drive their own signal handling; this just adds an
    explicit audit flush so no buffered record is lost at shutdown.
    """

    def _flush() -> None:
        for handler in logging.getLogger("perplexity_agent.audit").handlers:
            try:
                handler.flush()
            except (ValueError, OSError):
                pass  # stream already closed (e.g. at interpreter shutdown) — nothing to flush

    atexit.register(_flush)

    def _on_sigterm(signum: int, _frame: object) -> None:
        _flush()
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)  # re-raise with default disposition

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:
        pass  # not on the main thread (e.g. under a test runner) — nothing to install


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
    _install_graceful_shutdown()

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
        _run_stdio(mcp)


if __name__ == "__main__":
    main()
