"""The `BugSource` boundary. SPECS.md §3.2, docs/DECISIONS.md 002.

Two methods and two implementations. This abstraction exists because Decision 002 hangs an
11:00 go/no-go on swapping `ReplayQASource` for `PlaywrightSource`; the protocol is that
decision encoded in a type, so failing the gate costs one class rather than a redesign.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models import RawFinding


@runtime_checkable
class BugSource(Protocol):
    """Anything that can turn a URL into candidate findings."""

    name: str

    async def available(self) -> bool:
        """True if this source can actually run right now.

        Checked before `scan`, so the 11:00 gate is a runtime question rather than a
        code change.
        """
        ...

    async def scan(self, url: str, scan_id: str) -> list[RawFinding]:
        """Explore `url` and return candidate findings.

        Must not raise on a merely-unproductive scan: zero findings is a real result, and
        `docs/AGENTS.md` instructs Scout to report a clean app plainly rather than
        manufacture findings.
        """
        ...
