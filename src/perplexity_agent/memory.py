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

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @classmethod
    def from_settings(cls, settings: Settings) -> Store:
        path = settings.store_path or str(default_store_path())
        return cls(path)

    def close(self) -> None:
        self._conn.close()

    # --- conversations -----------------------------------------------------
    def add_message(self, role: str, content: str, *, now: float, space: str = "default") -> None:
        self._conn.execute(
            "INSERT INTO conversations (space, role, content, created) VALUES (?, ?, ?, ?)",
            (space, role, content, now),
        )
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
        self._conn.execute(
            "INSERT INTO tabs (space, title, url, kind, text, created) VALUES (?, ?, ?, ?, ?, ?)",
            (space, tab.title, tab.url, tab.kind, tab.text, now),
        )
        self._conn.commit()

    def tabs(self, *, space: str = "default", limit: int = 50) -> list[StoredTab]:
        """Return up to ``limit`` most-recent tabs, deduped by URL (newest wins).

        Bounds what ``/space`` reloads into memory and the prompt context: the
        tabs table is append-only (one row per ``/open``), so without a cap a
        long-lived space would grow without limit.
        """
        rows = self._conn.execute(
            "SELECT title, url, kind, text FROM tabs WHERE space = ? ORDER BY id DESC LIMIT ?",
            (space, limit),
        ).fetchall()
        seen: set[str] = set()
        recent: list[StoredTab] = []
        for r in rows:  # rows are newest-first; keep the first sighting of each URL
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            recent.append(StoredTab(r["title"], r["url"], r["kind"], r["text"]))
        recent.reverse()  # back to chronological order for display
        return recent

    # --- spaces ------------------------------------------------------------
    def create_space(self, name: str, *, now: float) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO spaces (name, created) VALUES (?, ?)", (name, now)
        )
        self._conn.commit()

    def spaces(self) -> list[str]:
        rows = self._conn.execute("SELECT name FROM spaces ORDER BY name").fetchall()
        return [r["name"] for r in rows]
