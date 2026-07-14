"""Local persistence for the TUI: conversation history, saved tabs, and Spaces.

A dependency-free :mod:`sqlite3` store standing in for Comet's memory + Spaces. It
lives under an XDG-style data directory by default (overridable via
``PERPLEXITY_STORE_PATH``) and holds only the user's own browsing/chat artifacts —
no secrets. All writes go through parameterized queries.

Hygiene: the database file is chmod'd to owner-only (0600) because it holds
browsing history; tabs are deduplicated per ``(space, url)`` on save (re-opening a
page replaces the stored copy instead of accumulating duplicates); and each space
keeps at most ``max_tabs_per_space`` tabs (and, when configured, at most
``max_history_per_space`` conversation rows) so growth is bounded.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    space     TEXT NOT NULL DEFAULT 'default',
    role      TEXT NOT NULL,
    content   TEXT NOT NULL,
    created   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tabs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    space     TEXT NOT NULL DEFAULT 'default',
    title     TEXT NOT NULL,
    url       TEXT NOT NULL,
    kind      TEXT NOT NULL DEFAULT 'page',
    text      TEXT NOT NULL DEFAULT '',
    created   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS spaces (
    name      TEXT PRIMARY KEY,
    created   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_responses (
    response_id TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created     REAL NOT NULL,
    PRIMARY KEY (response_id, session_id)
);
-- history()/tabs() and the retention/dedup queries all filter by space (and url);
-- index them so the lookups stay fast as these tables grow.
CREATE INDEX IF NOT EXISTS idx_conversations_space ON conversations(space, id);
CREATE INDEX IF NOT EXISTS idx_tabs_space ON tabs(space, id);
CREATE INDEX IF NOT EXISTS idx_tabs_space_url ON tabs(space, url);
CREATE INDEX IF NOT EXISTS idx_agent_responses_session
    ON agent_responses(session_id, created);
-- Retire the legacy `facts` table. It was never read or written, so dropping it
-- destroys no user data; this just cleans it out of stores created earlier.
DROP TABLE IF EXISTS facts;
"""

# Default cap when constructed without settings (e.g. in tests): a bounded
# recent-tab cache, matching the historical default.
_DEFAULT_MAX_TABS_PER_SPACE = 50


def default_store_path() -> Path:
    """XDG-style default location for the store database."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return Path(base) / "perplexity-agent" / "store.db"


@dataclass
class StoredTab:
    title: str
    url: str
    kind: str
    text: str


class Store:
    """Thin sqlite wrapper for TUI history, tabs, and Spaces.

    ``now`` is injected (no implicit clock) so callers — and tests — control
    timestamps; the TUI passes ``time.time``.

    Retention is per Space: ``max_tabs_per_space`` bounds the recent-tab cache
    (default 50), and ``max_history_per_space`` optionally bounds conversation
    history (``None`` = keep everything, so chat history is never auto-deleted
    unless an operator opts in).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_tabs_per_space: int | None = _DEFAULT_MAX_TABS_PER_SPACE,
        max_history_per_space: int | None = None,
        max_responses_per_session: int = 100,
    ) -> None:
        self._path = Path(path)
        self._max_tabs = max_tabs_per_space
        self._max_history = max_history_per_space
        self._max_responses = max_responses_per_session
        on_disk = str(self._path) != ":memory:"
        if on_disk:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        if on_disk:
            # Browsing history/page text: owner-only, like a browser profile.
            self._path.chmod(0o600)

    @classmethod
    def from_settings(cls, settings: Settings) -> Store:
        path = settings.store_path or str(default_store_path())
        return cls(
            path,
            max_tabs_per_space=settings.max_tabs_per_space,
            max_history_per_space=settings.max_history_per_space,
            max_responses_per_session=settings.max_responses_per_session,
        )

    def close(self) -> None:
        self._conn.close()

    def _prune(self, table: str, space: str, keep: int | None) -> None:
        """Delete all but the ``keep`` most-recent rows for ``space`` in ``table``.

        No-op when ``keep`` is None (retention disabled). ``table`` is never user
        input — it is a fixed literal from the caller — so the f-string is safe.
        """
        if keep is None:
            return
        self._conn.execute(
            f"DELETE FROM {table} WHERE space = ? AND id NOT IN "  # noqa: S608 - fixed table name
            f"(SELECT id FROM {table} WHERE space = ? ORDER BY id DESC LIMIT ?)",
            (space, space, keep),
        )

    # --- conversations -----------------------------------------------------
    def add_message(self, role: str, content: str, *, now: float, space: str = "default") -> None:
        self._conn.execute(
            "INSERT INTO conversations (space, role, content, created) VALUES (?, ?, ?, ?)",
            (space, role, content, now),
        )
        self._prune("conversations", space, self._max_history)
        self._conn.commit()

    def history(self, *, space: str = "default", limit: int = 50) -> list[dict[str, str]]:
        rows = self._conn.execute(
            "SELECT role, content FROM conversations WHERE space = ? "
            "ORDER BY id DESC LIMIT ?",
            (space, limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # --- tabs --------------------------------------------------------------
    def save_tab(self, tab: StoredTab, *, now: float, space: str = "default") -> None:
        # Dedupe per (space, url): re-opening a page replaces the stored copy.
        self._conn.execute(
            "DELETE FROM tabs WHERE space = ? AND url = ?", (space, tab.url)
        )
        self._conn.execute(
            "INSERT INTO tabs (space, title, url, kind, text, created) VALUES (?, ?, ?, ?, ?, ?)",
            (space, tab.title, tab.url, tab.kind, tab.text, now),
        )
        # Retention: keep only the newest max_tabs_per_space tabs in this space.
        self._prune("tabs", space, self._max_tabs)
        self._conn.commit()

    def tabs(self, *, space: str = "default") -> list[StoredTab]:
        rows = self._conn.execute(
            "SELECT title, url, kind, text FROM tabs WHERE space = ? ORDER BY id",
            (space,),
        ).fetchall()
        return [StoredTab(r["title"], r["url"], r["kind"], r["text"]) for r in rows]

    # --- spaces ------------------------------------------------------------
    def create_space(self, name: str, *, now: float) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO spaces (name, created) VALUES (?, ?)", (name, now)
        )
        self._conn.commit()

    def spaces(self) -> list[str]:
        rows = self._conn.execute("SELECT name FROM spaces ORDER BY name").fetchall()
        return [r["name"] for r in rows]

    # --- Agent API responses ----------------------------------------------
    def save_response(
        self,
        response_id: str,
        payload: dict[str, Any],
        *,
        session_id: str,
        now: float,
    ) -> None:
        """Persist a response snapshot, scoped to the originating MCP session."""
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        self._conn.execute(
            "INSERT OR REPLACE INTO agent_responses "
            "(response_id, session_id, payload, created) VALUES (?, ?, ?, ?)",
            (response_id, session_id, encoded, now),
        )
        self._conn.execute(
            "DELETE FROM agent_responses WHERE session_id = ? AND rowid NOT IN "
            "(SELECT rowid FROM agent_responses WHERE session_id = ? "
            "ORDER BY created DESC LIMIT ?)",
            (session_id, session_id, self._max_responses),
        )
        self._conn.commit()

    def response(self, response_id: str, *, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload FROM agent_responses WHERE response_id = ? AND session_id = ?",
            (response_id, session_id),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row["payload"])
        return value if isinstance(value, dict) else None

    def response_owner(self, response_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT session_id FROM agent_responses WHERE response_id = ? LIMIT 1",
            (response_id,),
        ).fetchone()
        return None if row is None else str(row["session_id"])
