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


def test_tabs_dedupe_by_url_newest_wins(tmp_path):
    # Re-opening the same URL must not stack duplicate rows in the returned view;
    # the most recent title/text for a URL wins, order stays chronological.
    s = _store(tmp_path)
    s.save_tab(StoredTab("Old", "https://x", "page", "v1"), now=1.0)
    s.save_tab(StoredTab("New", "https://x", "page", "v2"), now=2.0)
    s.save_tab(StoredTab("Other", "https://y", "page", "z"), now=3.0)
    tabs = s.tabs()
    assert [t.url for t in tabs] == ["https://x", "https://y"]
    assert tabs[0].title == "New" and tabs[0].text == "v2"
    s.close()


def test_tabs_limit_caps_rows(tmp_path):
    s = _store(tmp_path)
    for i in range(10):
        s.save_tab(StoredTab(f"T{i}", f"https://x/{i}", "page", "b"), now=float(i))
    tabs = s.tabs(limit=3)
    assert len(tabs) == 3
    assert [t.url for t in tabs] == ["https://x/7", "https://x/8", "https://x/9"]
    s.close()


def test_retention_disabled_by_default_keeps_everything(tmp_path):
    # The safe default: no retention cap means a deployed instance never deletes
    # a user's history.
    s = _store(tmp_path)
    for i in range(50):
        s.add_message("user", f"m{i}", now=float(i))
    count = s._conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
    assert count == 50
    s.close()


def test_history_retention_prunes_oldest_per_space(tmp_path):
    # With a cap, only the N most-recent rows per Space survive; pruning is
    # scoped to the Space being written, never another's.
    s = Store(tmp_path / "store.db", max_history_per_space=3)
    for i in range(6):
        s.add_message("user", f"d{i}", now=float(i), space="default")
    s.add_message("user", "keep-me", now=99.0, space="work")
    default_rows = s._conn.execute(
        "SELECT content FROM conversations WHERE space = 'default' ORDER BY id"
    ).fetchall()
    assert [r["content"] for r in default_rows] == ["d3", "d4", "d5"]
    work_rows = s._conn.execute(
        "SELECT content FROM conversations WHERE space = 'work'"
    ).fetchall()
    assert [r["content"] for r in work_rows] == ["keep-me"]  # other Space untouched
    s.close()


def test_tabs_retention_prunes_oldest(tmp_path):
    s = Store(tmp_path / "store.db", max_tabs_per_space=2)
    for i in range(5):
        s.save_tab(StoredTab(f"T{i}", f"https://x/{i}", "page", "b"), now=float(i))
    rows = s._conn.execute("SELECT url FROM tabs ORDER BY id").fetchall()
    assert [r["url"] for r in rows] == ["https://x/3", "https://x/4"]
    s.close()


def test_legacy_facts_table_is_dropped(tmp_path):
    # A store created with an old schema (with a facts table) is cleaned up on open.
    import sqlite3

    db = tmp_path / "store.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, fact TEXT, created REAL)")
    conn.execute("INSERT INTO facts (fact, created) VALUES ('stale', 1.0)")
    conn.commit()
    conn.close()
    s = Store(db)  # opening runs the schema, which drops the legacy table
    names = {
        r["name"]
        for r in s._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "facts" not in names
    s.close()


def test_space_lookup_indexes_exist(tmp_path):
    # history()/tabs() filter by space; the supporting indexes must be created so
    # the lookups don't degrade to full scans as the tables grow.
    s = _store(tmp_path)
    names = {
        r["name"]
        for r in s._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert {"idx_conversations_space", "idx_tabs_space"} <= names
    s.close()


def test_spaces_create_is_idempotent(tmp_path):
    s = _store(tmp_path)
    s.create_space("research", now=1.0)
    s.create_space("research", now=2.0)  # idempotent
    assert "research" in s.spaces()
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
