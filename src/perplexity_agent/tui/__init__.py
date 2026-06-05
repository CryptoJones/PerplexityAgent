"""Interactive terminal UI ("Comet in the terminal") for PerplexityAgent.

Importing this package pulls in Textual, which is an optional dependency (the
``tui`` extra). ``__main__`` imports it lazily so a core install without the extra
still runs the MCP server and only the ``tui`` subcommand reports the missing extra.
"""

from __future__ import annotations

from .app import CometApp, run_tui

__all__ = ["CometApp", "run_tui"]
