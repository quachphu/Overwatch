"""`SeedSource` — the 12:30 spine-gate escape hatch.

SPECS.md §8: *"Gate: if not met, switch to a pre-scanned fallback app and move on."* This is
that fallback. It replays a fixture of findings from a real earlier scan so that the two
Terac rounds — which are two thirds of the rubric and must never be cut — can launch on
schedule even if the live scanner is failing on the chosen URL.

**This is not a mock of a bug source; it is a recorded one.** It is only selected when
`BUG_SOURCE=seed` is set explicitly, it labels every finding `source="seed"`, and the
dashboard and report templates surface that label, so a seeded run can never be presented
as a live scan. Screenshots are generated placeholders that say so on their face.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from app.models import RawFinding
from app.sources.evidence import save_screenshot

logger = logging.getLogger(__name__)

FIXTURE = Path(__file__).parent / "seed_findings.json"


def _placeholder(caption: str) -> bytes:
    """A placeholder frame that states what it is, in words, to the person looking at it.

    This replaces a hand-written 1x1 PNG hex literal that was silently **corrupt** — its IDAT
    chunk length was off by one, so `file(1)` reported a valid PNG (it reads only IHDR) while
    every real decoder rejected it and every participant saw a broken-image icon.

    SVG rather than PNG because the honesty claim in this module's docstring requires legible
    text, and hand-rolling a text-rendering PNG encoder to say one sentence is not a trade worth
    making. `<img src="...svg">` renders in every browser a Terac participant will have.
    """
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800" '
        'viewBox="0 0 1280 800" role="img" aria-label="Sample evidence, not a live capture">'
        '<rect width="1280" height="800" fill="#12161c"/>'
        '<rect x="24" y="24" width="1232" height="752" fill="none" stroke="#2b3441" '
        'stroke-width="2" stroke-dasharray="12 10"/>'
        '<text x="640" y="372" fill="#e6edf7" font-family="ui-sans-serif,system-ui,sans-serif" '
        'font-size="52" font-weight="700" text-anchor="middle">SAMPLE — not a live capture</text>'
        f'<text x="640" y="438" fill="#8b98a9" font-family="ui-monospace,monospace" '
        f'font-size="26" text-anchor="middle">{caption}</text>'
        '<text x="640" y="500" fill="#8b98a9" font-family="ui-sans-serif,system-ui,sans-serif" '
        'font-size="24" text-anchor="middle">Judge this finding from its written description.</text>'
        "</svg>"
    ).encode()


class SeedSource:
    """Replays a recorded finding set. Selected only by explicit configuration."""

    name = "seed"

    async def available(self) -> bool:
        """Whether the fixture exists *and* parses. `exists()` alone reported a truncated or
        hand-edited file as usable, which is the worst moment to discover otherwise."""
        try:
            return isinstance(json.loads(FIXTURE.read_text()), list)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Seed fixture at %s is unusable: %s", FIXTURE, exc)
            return False

    async def scan(self, url: str, scan_id: str) -> list[RawFinding]:
        if not FIXTURE.exists():
            logger.error("Seed fixture missing at %s", FIXTURE)
            return []

        try:
            rows = json.loads(FIXTURE.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            # This is the path whose entire job is to work when everything else is broken, on a
            # file someone may hand-edit under time pressure. It must not raise.
            logger.error("Seed fixture at %s is unreadable: %s", FIXTURE, exc)
            return []

        findings: list[RawFinding] = []

        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                logger.warning("Skipping seed row %d: not an object.", index)
                continue
            before = save_screenshot(
                scan_id, f"seed-{index}-before.svg", _placeholder("state before the step")
            )
            after = save_screenshot(
                scan_id, f"seed-{index}-after.svg", _placeholder("state after the step")
            )
            try:
                findings.append(
                    RawFinding(
                        scan_id=scan_id,
                        journey=row["journey"],
                        step_intent=row["step_intent"],
                        expected=row["expected"],
                        observed=row["observed"],
                        screenshot_before_url=before,
                        screenshot_after_url=after,
                        console_errors=row.get("console_errors", []),
                        failed_requests=row.get("failed_requests", []),
                        source="seed",
                        agent_severity=row["agent_severity"],
                        agent_confidence=row["agent_confidence"],
                        category=row.get("category", "uncategorized"),
                    )
                )
            except (KeyError, ValidationError) as exc:
                logger.warning("Skipping malformed seed row %d: %s", index, exc)
                continue

        logger.warning(
            "SeedSource replayed %d recorded findings for %s. This is a FALLBACK run and is "
            "labelled as such in the report.",
            len(findings),
            url,
        )
        return findings
