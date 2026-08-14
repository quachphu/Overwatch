"""Bursar — takes the money, starts the scan, releases the report.

Holds no tool that can lift a veto. `release` returns released:false while any Critic veto is
open and takes no argument that changes that, so the prompt's "you never override Critic" is a
property of the toolset rather than a request.
"""

from __future__ import annotations

from app.agents.runtime import main

if __name__ == "__main__":
    raise SystemExit(main(["bursar"]))
