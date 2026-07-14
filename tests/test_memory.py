from pydantic import SecretStr

from perplexity_agent.config import Settings
from perplexity_agent.memory import Store, StoredTab, default_store_path


def _store(tmp_path):
    return Store(tmp_path / "store.db")


def test_conversation_round_trip(tmp_path):
    s = _store(tmp_path)
    s.add_message("user", "hi", now=1.0)
    s.add_message("assistant", "hello", now=2.0)
    hist = s.history()
    assert hist == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    s.close()


def test_history_is_scoped_by_space(tmp_path):
    s = _store(tmp_path)
    s.add_message("user", "a", now=1.0, space="default")
    s.add_message("user", "b", now=2.0, space="work")
    assert [m["content"] for m in s.history(space="default")] == ["a"]
    assert [m["content"] for m in s.history(space="work")] == ["b"]
    s.close()


def test_tabs_round_trip(tmp_path):
    s = _store(tmp_path)
    s.save_tab(StoredTab("T", "https://x", "page", "body"), now=1.0)
    tabs = s.tabs()
    assert len(tabs) == 1
    assert tabs[0].title == "T"
    assert tabs[0].text == "body"
    s.close()


def test_save_tab_dedupes_by_space_and_url(tmp_path):
    s = _store(tmp_path)
    s.save_tab(StoredTab("Old title", "https://x", "page", "old"), now=1.0)
    s.save_tab(StoredTab("New title", "https://x", "page", "new"), now=2.0)
    tabs = s.tabs()
    assert len(tabs) == 1  # replaced, not accumulated
    assert tabs[0].title == "New title"
    assert tabs[0].text == "new"
    # Same URL in a different space is its own tab.
    s.save_tab(StoredTab("Work copy", "https://x", "page", "w"), now=3.0, space="work")
    assert len(s.tabs(space="work")) == 1
    assert len(s.tabs()) == 1
    s.close()


def test_tabs_retention_cap_per_space(tmp_path):
    from perplexity_agent.memory import _DEFAULT_MAX_TABS_PER_SPACE

    s = _store(tmp_path)
    for i in range(_DEFAULT_MAX_TABS_PER_SPACE + 5):
        s.save_tab(StoredTab(f"T{i}", f"https://x/{i}", "page", "b"), now=float(i))
    tabs = s.tabs()
    assert len(tabs) == _DEFAULT_MAX_TABS_PER_SPACE
    # The oldest tabs were pruned; the newest survive.
    assert tabs[0].title == "T5"
    assert tabs[-1].title == f"T{_DEFAULT_MAX_TABS_PER_SPACE + 4}"
    # Other spaces are untouched by the cap.
    s.save_tab(StoredTab("W", "https://w", "page", "b"), now=999.0, space="work")
    assert len(s.tabs(space="work")) == 1
    s.close()


def test_store_file_is_owner_only(tmp_path):
    import os
    import stat

    if os.name == "nt":  # pragma: no cover - POSIX permissions only
        return
    s = _store(tmp_path)
    mode = stat.S_IMODE(os.stat(tmp_path / "store.db").st_mode)
    assert mode == 0o600
    s.close()


def test_spaces_create_is_idempotent(tmp_path):
    s = _store(tmp_path)
    s.create_space("research", now=1.0)
    s.create_space("research", now=2.0)  # idempotent
    assert "research" in s.spaces()
    s.close()


def test_history_unbounded_by_default(tmp_path):
    # The safe default: no history cap means chat history is never auto-deleted.
    s = _store(tmp_path)
    for i in range(60):
        s.add_message("user", f"m{i}", now=float(i))
    count = s._conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
    assert count == 60
    s.close()


def test_history_retention_prunes_oldest_per_space_when_configured(tmp_path):
    s = Store(tmp_path / "store.db", max_history_per_space=3)
    for i in range(6):
        s.add_message("user", f"d{i}", now=float(i), space="default")
    s.add_message("user", "keep-me", now=99.0, space="work")
    default_rows = [m["content"] for m in s.history(space="default", limit=99)]
    assert default_rows == ["d3", "d4", "d5"]
    assert [m["content"] for m in s.history(space="work")] == ["keep-me"]  # other Space untouched
    s.close()


def test_tabs_cap_is_configurable(tmp_path):
    s = Store(tmp_path / "store.db", max_tabs_per_space=2)
    for i in range(5):
        s.save_tab(StoredTab(f"T{i}", f"https://x/{i}", "page", "b"), now=float(i))
    assert [t.url for t in s.tabs()] == ["https://x/3", "https://x/4"]
    s.close()


def test_space_lookup_indexes_exist(tmp_path):
    s = _store(tmp_path)
    names = {
        r["name"]
        for r in s._conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    }
    assert {
        "idx_conversations_space",
        "idx_tabs_space",
        "idx_tabs_space_url",
        "idx_agent_responses_session",
    } <= names
    s.close()


def test_legacy_facts_table_is_dropped(tmp_path):
    import sqlite3

    db = tmp_path / "store.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, fact TEXT, created REAL)")
    conn.commit()
    conn.close()
    s = Store(db)  # opening runs the schema, which drops the legacy table
    names = {
        r["name"]
        for r in s._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert "facts" not in names
    s.close()


def test_from_settings_uses_store_path(tmp_path):
    settings = Settings(api_key=SecretStr("pplx-x"), store_path=str(tmp_path / "db.sqlite"))
    s = Store.from_settings(settings)
    s.add_message("user", "q", now=1.0)
    assert (tmp_path / "db.sqlite").exists()
    s.close()


def test_default_store_path_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    p = default_store_path()
    assert p == tmp_path / "perplexity-agent" / "store.db"


def test_agent_response_round_trip_is_session_scoped(tmp_path):
    s = _store(tmp_path)
    payload = {"id": "resp_1", "object": "response", "output": []}
    s.save_response("resp_1", payload, session_id="session-a", now=1.0)
    assert s.response("resp_1", session_id="session-a") == payload
    assert s.response("resp_1", session_id="session-b") is None
    assert s.response_owner("resp_1") == "session-a"
    s.close()


def test_agent_response_retention_is_bounded_per_session(tmp_path):
    s = Store(tmp_path / "store.db", max_responses_per_session=2)
    for i in range(4):
        response_id = f"resp_{i}"
        s.save_response(response_id, {"id": response_id}, session_id="session-a", now=float(i))
    s.save_response("resp_other", {"id": "resp_other"}, session_id="session-b", now=9.0)
    assert s.response("resp_0", session_id="session-a") is None
    assert s.response("resp_2", session_id="session-a") is not None
    assert s.response("resp_3", session_id="session-a") is not None
    assert s.response("resp_other", session_id="session-b") is not None
    s.close()
