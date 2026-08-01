import sys

import httpx
import pytest
from pydantic import SecretStr

import perplexity_agent.__main__ as cli
from perplexity_agent.config import Settings


@pytest.fixture
def fake_settings(monkeypatch):
    s = Settings(api_key=SecretStr("pplx-x"))
    monkeypatch.setattr(cli, "load_settings", lambda: s)
    return s


def test_main_tui_subcommand_dispatches(monkeypatch, fake_settings):
    called = {}
    monkeypatch.setattr(cli, "_run_tui", lambda settings: called.setdefault("tui", settings))
    monkeypatch.setattr(sys, "argv", ["perplexity-agent", "tui"])
    cli.main()
    assert called["tui"] is fake_settings


def test_main_stdio_is_default(monkeypatch, fake_settings):
    ran = {}

    fake_mcp = object()
    monkeypatch.setattr(cli, "build_server", lambda s: (fake_mcp, s))
    monkeypatch.setattr(
        cli,
        "_run_stdio",
        lambda mcp: ran.setdefault("mcp", mcp),
    )
    monkeypatch.setattr(sys, "argv", ["perplexity-agent"])
    cli.main()
    assert ran["mcp"] is fake_mcp


def test_main_http_dispatches(monkeypatch, fake_settings):
    ran = {}
    monkeypatch.setattr(cli, "build_server", lambda s: (object(), s))
    monkeypatch.setattr(cli, "_run_http", lambda mcp, s: ran.setdefault("http", True))
    monkeypatch.setattr(sys, "argv", ["perplexity-agent", "--transport", "http"])
    cli.main()
    assert ran["http"] is True


async def test_run_http_enforces_bearer_auth(monkeypatch):
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def endpoint(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/", endpoint)])

    class _FakeMCP:
        def streamable_http_app(self):
            return app

    captured = {}

    def fake_run(asgi_app, *, host, port):
        captured.update(app=asgi_app, host=host, port=port)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    settings = Settings(
        api_key=SecretStr("pplx-x"),
        http_auth_token=SecretStr("transport-secret"),
        http_host="127.0.0.1",
        http_port=8765,
    )
    cli._run_http(_FakeMCP(), settings)

    transport = httpx.ASGITransport(app=captured["app"])
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/")
        allowed = await client.get("/", headers={"Authorization": "Bearer transport-secret"})
    assert denied.status_code == 401
    assert allowed.json() == {"ok": True}
    assert (captured["host"], captured["port"]) == ("127.0.0.1", 8765)


def test_run_http_refuses_missing_auth_token(fake_settings):
    with pytest.raises(SystemExit, match="PERPLEXITY_HTTP_AUTH_TOKEN"):
        cli._run_http(object(), fake_settings)


def test_main_transport_with_tui_is_rejected(monkeypatch, fake_settings):
    # `--transport http tui` must error, not silently discard the flag and start
    # an interactive UI on a headless host.
    ran = {}
    monkeypatch.setattr(cli, "_run_tui", lambda settings: ran.setdefault("tui", True))
    monkeypatch.setattr(sys, "argv", ["perplexity-agent", "--transport", "http", "tui"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2  # argparse usage error
    assert "tui" not in ran


def test_stdio_server_exits_on_client_eof(tmp_path):
    """Lifecycle regression: when the MCP client closes the stdio pipe, the
    server process must exit instead of lingering deaf.

    A stdio MCP server that outlives its transport silently discards every
    request and the client hangs forever (this exact failure mode shipped in
    obsidian-mcp; see omind#49). Locks in that nothing in this server — future
    background tasks, watchers, threads — ever keeps the process alive past
    its client.
    """
    import json
    import os
    import subprocess

    env = {
        **os.environ,
        "PERPLEXITY_API_KEY": "pplx-dummy-eof-test",
        "PERPLEXITY_STORE_PATH": str(tmp_path / "store.db"),
    }
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, test-controlled
        [sys.executable, "-m", "perplexity_agent"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=str(tmp_path),  # keep any repo-local .env out of the picture
    )
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "eof-test", "version": "0"},
        },
    }
    try:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(init) + "\n").encode())
        proc.stdin.flush()
        proc.stdin.close()  # the client goes away
        rc = proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert rc == 0


def test_stdio_survives_a_request_larger_than_64k(tmp_path):
    """Robustness regression: a JSON-RPC line over asyncio's default 64 KiB
    StreamReader limit must not crash the transport.

    The stdin reader used a default-limit ``asyncio.StreamReader``; a >64K line
    raised ``LimitOverrunError`` and tore down the whole task group, killing the
    server on a single large (legitimate or hostile) request. The reader now uses
    a 16 MiB limit, so a large-but-reasonable request is served and the process
    stays alive to answer the next one.
    """
    import json
    import os
    import subprocess

    env = {
        **os.environ,
        "PERPLEXITY_API_KEY": "pplx-dummy-big-line",
        "PERPLEXITY_STORE_PATH": str(tmp_path / "store.db"),
    }
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, test-controlled
        [sys.executable, "-m", "perplexity_agent"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=str(tmp_path),
    )

    def send(obj):
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        proc.stdin.flush()

    try:
        # server_metrics ignores the oversized extra field; the point is the
        # ~100 KB line (well over 64 KiB) does not crash the reader.
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "server_metrics",
                    "arguments": {"__unexpected__": "x" * 100_000},
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            }
        )
        assert proc.stdout is not None
        line = proc.stdout.readline()  # a reply, not a dead pipe
        assert line, "server produced no reply to an over-64K request (transport crashed)"
        reply = json.loads(line)
        assert reply.get("id") == 1
        assert "result" in reply and "error" not in reply
        # And it is still alive for the next request.
        proc.stdin.close()
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_run_tui_reports_missing_extra(monkeypatch, fake_settings):
    # Simulate the `tui` extra not being installed: a None entry in sys.modules
    # makes `from .tui import run_tui` raise ImportError without touching the real
    # import machinery for anything else.
    monkeypatch.setitem(sys.modules, "perplexity_agent.tui", None)
    with pytest.raises(SystemExit) as exc:
        cli._run_tui(fake_settings)
    assert "tui" in str(exc.value)


def test_graceful_shutdown_flushes_on_exit_and_sigterm(monkeypatch):
    import logging
    import signal

    callbacks = {}
    installed = {}
    raised = []

    class _FlushCounter(logging.Handler):
        def __init__(self):
            super().__init__()
            self.flushes = 0

        def emit(self, _record):
            pass

        def flush(self):
            self.flushes += 1

    handler = _FlushCounter()
    audit_logger = logging.getLogger("perplexity_agent.audit")
    audit_logger.addHandler(handler)
    monkeypatch.setattr(
        cli.atexit,
        "register",
        lambda callback: callbacks.setdefault("exit", callback),
    )
    monkeypatch.setattr(
        cli.signal,
        "signal",
        lambda signum, callback: installed.setdefault(signum, callback),
    )
    monkeypatch.setattr(cli.signal, "raise_signal", raised.append)
    try:
        cli._install_graceful_shutdown()
        callbacks["exit"]()
        installed[signal.SIGTERM](signal.SIGTERM, None)
    finally:
        audit_logger.removeHandler(handler)

    assert handler.flushes >= 2
    assert raised == [signal.SIGTERM]
