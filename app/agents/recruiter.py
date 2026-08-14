"""Recruiter — the only agent that talks to Terac.

The Terac key reaches no other agent's toolset (app/agents/tools.py, TOOLSETS). Round 2 is
launched only when the has_not_taken_study filter excluding round 1 can be constructed;
app.pipeline.launch_round2 refuses otherwise rather than launching a contaminated panel.
"""

from __future__ import annotations

from app.agents.runtime import main

if __name__ == "__main__":
    raise SystemExit(main(["recruiter"]))
