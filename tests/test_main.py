import sys

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

    class _FakeMCP:
        def run(self, transport):
            ran["transport"] = transport

    monkeypatch.setattr(cli, "build_server", lambda s: (_FakeMCP(), s))
    monkeypatch.setattr(sys, "argv", ["perplexity-agent"])
    cli.main()
    assert ran["transport"] == "stdio"


def test_main_http_dispatches(monkeypatch, fake_settings):
    ran = {}
    monkeypatch.setattr(cli, "build_server", lambda s: (object(), s))
    monkeypatch.setattr(cli, "_run_http", lambda mcp, s: ran.setdefault("http", True))
    monkeypatch.setattr(sys, "argv", ["perplexity-agent", "--transport", "http"])
    cli.main()
    assert ran["http"] is True


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

    env = {**os.environ, "PERPLEXITY_API_KEY": "pplx-dummy-eof-test"}
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


def test_run_tui_reports_missing_extra(monkeypatch, fake_settings):
    # Simulate the `tui` extra not being installed: a None entry in sys.modules
    # makes `from .tui import run_tui` raise ImportError without touching the real
    # import machinery for anything else.
    monkeypatch.setitem(sys.modules, "perplexity_agent.tui", None)
    with pytest.raises(SystemExit) as exc:
        cli._run_tui(fake_settings)
    assert "tui" in str(exc.value)
