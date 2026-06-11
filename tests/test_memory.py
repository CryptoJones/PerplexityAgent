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
    from perplexity_agent.memory import _MAX_TABS_PER_SPACE

    s = _store(tmp_path)
    for i in range(_MAX_TABS_PER_SPACE + 5):
        s.save_tab(StoredTab(f"T{i}", f"https://x/{i}", "page", "b"), now=float(i))
    tabs = s.tabs()
    assert len(tabs) == _MAX_TABS_PER_SPACE
    # The oldest tabs were pruned; the newest survive.
    assert tabs[0].title == "T5"
    assert tabs[-1].title == f"T{_MAX_TABS_PER_SPACE + 4}"
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


def test_spaces_and_facts(tmp_path):
    s = _store(tmp_path)
    s.create_space("research", now=1.0)
    s.create_space("research", now=2.0)  # idempotent
    assert "research" in s.spaces()
    s.remember("user prefers concise answers", now=1.0)
    assert s.facts() == ["user prefers concise answers"]
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
