"""Local persistence for the TUI: conversation history, saved tabs, and Spaces.

A dependency-free :mod:`sqlite3` store standing in for Comet's memory + Spaces. It
lives under an XDG-style data directory by default (overridable via
``PERPLEXITY_STORE_PATH``) and holds only the user's own browsing/chat artifacts —
no secrets. All writes go through parameterized queries.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

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
-- history()/tabs() always filter by space and order by id; index both so the
-- lookups stay fast as these append-only tables grow.
CREATE INDEX IF NOT EXISTS idx_conversations_space ON conversations(space, id);
CREATE INDEX IF NOT EXISTS idx_tabs_space ON tabs(space, id);
-- A tab is identified by its URL within a Space: re-opening a URL updates that
-- tab in place (save_tab uses INSERT OR REPLACE) instead of stacking duplicate
-- rows. That keeps the row count, the tabs() LIMIT, and retention pruning all
-- tracking DISTINCT tabs. Collapse any pre-existing duplicates (newest row per
-- URL wins), then enforce uniqueness.
DELETE FROM tabs WHERE id NOT IN (SELECT MAX(id) FROM tabs GROUP BY space, url);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tabs_space_url ON tabs(space, url);
-- Retire the legacy `facts` table. It was never read or written, so dropping it
-- destroys no user data; this just cleans it out of stores created before it was
-- removed from the schema.
DROP TABLE IF EXISTS facts;
"""


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
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_history_per_space: int | None = None,
        max_tabs_per_space: int | None = None,
    ) -> None:
        self._path = Path(path)
        # Retention caps. None means keep everything: nothing is ever deleted
        # unless the operator explicitly opts in. This is the safe default for a
        # deployed multi-user instance.
        self._max_history = max_history_per_space
        self._max_tabs = max_tabs_per_space
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @classmethod
    def from_settings(cls, settings: Settings) -> Store:
        path = settings.store_path or str(default_store_path())
        return cls(
            path,
            max_history_per_space=settings.max_history_per_space,
            max_tabs_per_space=settings.max_tabs_per_space,
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
        # INSERT OR REPLACE on the UNIQUE(space, url) index: re-opening a URL
        # replaces its row (taking a fresh, higher id so it sorts as most-recent)
        # rather than stacking a duplicate. One row per distinct URL means the
        # LIMIT in tabs() and retention pruning both count distinct tabs.
        self._conn.execute(
            "INSERT OR REPLACE INTO tabs (space, title, url, kind, text, created) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (space, tab.title, tab.url, tab.kind, tab.text, now),
        )
        self._prune("tabs", space, self._max_tabs)
        self._conn.commit()

    def tabs(self, *, space: str = "default", limit: int = 50) -> list[StoredTab]:
        """Return the ``limit`` most-recent tabs for ``space``, in chronological order.

        save_tab keeps one row per distinct URL (UNIQUE(space, url)), so ``LIMIT``
        here returns up to ``limit`` *distinct* tabs — it bounds what ``/space``
        reloads into memory without a burst of re-opens crowding out other tabs.
        """
        rows = self._conn.execute(
            "SELECT title, url, kind, text FROM tabs WHERE space = ? ORDER BY id DESC LIMIT ?",
            (space, limit),
        ).fetchall()
        rows.reverse()  # newest-first from SQL -> chronological for display
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
