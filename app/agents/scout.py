"""Scout — finds bugs, judges nothing. Prompt in app/agents/prompts.py, verbatim.

Its own process and its own Band registration: one live WebSocket per agent_id, and a second
process on the same id silently drops the first.
"""

from __future__ import annotations

from app.agents.runtime import main

if __name__ == "__main__":
    raise SystemExit(main(["scout"]))
