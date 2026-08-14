"""Critic — can say no, and is the only agent that can.

Checks PII in evidence, the human confirmation rate, and overclaiming, then answers exactly
"BLOCKED: <reason>" or "CLEAR".

One honest limitation, stated here and in the tool's own docstring so the model repeats it
rather than overstating the check: `inspect_for_release` pre-screens *text* — console output and
observed strings — not screenshot pixels. SPECS.md §9 describes a vision pass over the images;
that is not implemented, so a name rendered inside a screenshot will not be caught
automatically.
"""

from __future__ import annotations

from app.agents.runtime import main

if __name__ == "__main__":
    raise SystemExit(main(["critic"]))
