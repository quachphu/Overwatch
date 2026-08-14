"""Triage Officer — ranks findings, decides which ones are worth paying a human for.

Owns report v1 and, after the labels land, report v2. Same findings both times; only the order
changes. That is the experiment.
"""

from __future__ import annotations

from app.agents.runtime import main

if __name__ == "__main__":
    raise SystemExit(main(["triage"]))
