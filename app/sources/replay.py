"""`ReplayQASource` — the preferred bug source (docs/DECISIONS.md 002).

**RESEARCH.md UNKNOWN 8 is resolved: Replay QA has a live REST API.** Verified this session by
fetching <https://qa.replay.io/api/v1/openapi.json> (HTTP 200, "Replay QA API 1.0.0").

What is verified, and what is not:

* **Verified** — server `https://qa.replay.io`, `bearerAuth` described as "API token from
  Settings > API Token (starts with lqa_)", and `POST /api/v1/projects` with `required: [name,
  target_url]`. Every optional field sent below appears in that request schema.
* **Verified by description, not schema** — the bug object's field names. The spec gives no
  response schema for `GET /projects/{id}/bugs` (only `description: "List of bugs"`), but the
  `webhook_url` field documents the exact bug payload: `{ body, referrer, callback_url, bug_id,
  title, severity, description, reproduction_steps, expected_behavior, actual_behavior,
  replay_recording_id, analysis, polish_category }`. `_to_finding` reads those names and
  tolerates their absence rather than raising, so a shape mismatch costs fields, not the run.
* **Not verified** — the project-id field on the create response. The spec documents only
  "Created project with exploration_id and url", so `_project_id` accepts several spellings and
  raises with the raw body if none appear.

Replay explores asynchronously, while `BugSource.scan` is awaited. We poll rather than take
webhooks: a webhook needs a public URL that the 11:00 gate cannot depend on, and polling keeps
this source usable from a laptop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.config import settings
from app.models import RawFinding, Severity

logger = logging.getLogger(__name__)

REPLAY_BASE_URL = settings.replay_base_url or "https://qa.replay.io"

# "Rough guide: ~10 = smoke test, 20-50 = thorough, 50+ = broad coverage. Defaults to 20 when
# omitted." We want breadth of *candidate* findings — human verification is what separates real
# from noise downstream — but not an unbounded spend during an 8-hour build.
DEFAULT_BUDGET = 20

# From the `enabled_polish_passes` enum in the create-project schema. These are the passes whose
# output a non-expert Terac participant can actually adjudicate from a screenshot; `seo` and
# `react-rendering` produce findings that need a developer to judge, which would poison round 1.
POLISH_PASSES = [
    "layout-shift",
    "accessibility",
    "glitches",
    "user-experience",
    "ui-details",
    "network-performance",
]

# Replay's severity vocabulary is not enumerated in the spec, so accept the usual spellings and
# fall back to `minor` rather than dropping a finding whose severity we do not recognise. Target
# vocabulary is ours: Severity = blocker | major | minor | cosmetic (app/models.py).
_SEVERITY_MAP: dict[str, Severity] = {
    "blocker": "blocker",
    "critical": "blocker",
    "high": "major",
    "major": "major",
    "medium": "minor",
    "moderate": "minor",
    "minor": "minor",
    "low": "minor",
    "cosmetic": "cosmetic",
    "trivial": "cosmetic",
    "info": "cosmetic",
    "informational": "cosmetic",
}

POLL_INTERVAL_SECONDS = 15
# The exploration must finish inside the build window. On timeout we return the bugs found so
# far — a partial scan is a usable scan; a raised exception is a dead 12:30 gate.
POLL_TIMEOUT_SECONDS = 600
# Stop once the bug count has been flat for this many consecutive polls, so a finished
# exploration does not burn the whole timeout.
STABLE_POLLS_TO_FINISH = 3


def _as_text(value: Any) -> str:
    """Flatten an undocumented field into prose a participant can read.

    These field shapes are inferred from the webhook payload docs, not verified, and
    `reproduction_steps` is plural — a list is the natural encoding. `str()` on a list yields
    `"['Open the homepage', 'Click Sign in']"`, brackets and quotes included, rendered verbatim
    into the one line a participant reads to understand what was tried. That makes the task look
    broken and depresses answer quality on judgments we paid for.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list | tuple):
        parts = [_as_text(v) for v in value]
        return "; ".join(p for p in parts if p)
    if isinstance(value, dict):
        parts = [_as_text(v) for v in value.values()]
        return "; ".join(p for p in parts if p)
    return str(value).strip()


class ReplayQASource:
    name = "replay"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key or settings.replay_api_key
        self.base_url = (base_url or REPLAY_BASE_URL).rstrip("/")

    async def available(self) -> bool:
        if not self.api_key:
            logger.info("ReplayQASource unavailable: REPLAY_API_KEY not set.")
            return False
        return True

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def scan(self, url: str, scan_id: str) -> list[RawFinding]:
        if not await self.available():
            raise RuntimeError(
                "ReplayQASource is not configured (REPLAY_API_KEY unset). The 11:00 gate "
                "fails over to PlaywrightSource; set BUG_SOURCE=playwright."
            )

        async with self._client() as client:
            project = await self._create_project(client, url=url, scan_id=scan_id)
            project_id = self._project_id(project)
            logger.info(
                "Replay project %s exploring %s — dashboard: %s",
                project_id,
                url,
                project.get("url"),
            )
            bugs = await self._await_bugs(client, project_id)

        findings = [self._to_finding(bug, scan_id) for bug in bugs]
        logger.info("Replay returned %d bugs for scan %s.", len(findings), scan_id)
        return findings

    async def _create_project(
        self, client: httpx.AsyncClient, *, url: str, scan_id: str
    ) -> dict[str, Any]:
        payload = {
            "name": f"Overwatch {scan_id}",
            "target_url": url,
            "budget": DEFAULT_BUDGET,
            "enabled_polish_passes": POLISH_PASSES,
            "instructions": (
                "Prioritise user-visible breakage on the primary flows a first-time visitor "
                "would attempt: navigation, forms, and search. Do not submit payments, delete "
                "data, or send messages."
            ),
        }
        response = await client.post("/api/v1/projects", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Replay {response.status_code} on POST /api/v1/projects: " f"{response.text[:500]}"
            )
        return response.json()

    @staticmethod
    def _project_id(project: dict[str, Any]) -> str:
        for key in ("project_id", "id", "projectId"):
            value = project.get(key)
            if value:
                return str(value)
        raise RuntimeError(
            "Replay create-project response carried no recognisable project id. "
            f"Body: {project!r}"
        )

    async def _await_bugs(self, client: httpx.AsyncClient, project_id: str) -> list[dict[str, Any]]:
        """Poll until the bug count stops moving, the project goes idle, or we run out of time."""
        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        bugs: list[dict[str, Any]] = []
        last_count = -1
        stable = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            result = await self._list_bugs(client, project_id)

            # A transient error must not be read as "the exploration found nothing". `bugs` was
            # reassigned unconditionally, and `_list_bugs` returned `[]` on any 4xx/5xx, so a
            # single 429 or proxy 502 on the *last* poll discarded every bug found in ten
            # minutes and logged "returning 0 bugs found so far". Downstream that becomes a
            # "clean" scan and round 1 cannot launch at all.
            if result is None:
                logger.warning("Replay bug list failed transiently; keeping %d found.", len(bugs))
                continue
            bugs = result

            if len(bugs) == last_count:
                stable += 1
                # Only call it finished once something was actually found; an exploration that
                # has not produced its first bug yet also looks "stable" at zero.
                if stable >= STABLE_POLLS_TO_FINISH and bugs:
                    return bugs
            else:
                stable = 0
                last_count = len(bugs)

        logger.warning(
            "Replay project %s still working after %ds — returning %d bugs found so far.",
            project_id,
            POLL_TIMEOUT_SECONDS,
            len(bugs),
        )
        return bugs

    async def _list_bugs(
        self, client: httpx.AsyncClient, project_id: str
    ) -> list[dict[str, Any]] | None:
        """The project's bugs, or `None` if the call failed.

        `None` rather than `[]` for a failure: the caller polls in a loop and must be able to
        tell "the request did not work" from "the exploration genuinely found nothing".
        """
        try:
            response = await client.get(f"/api/v1/projects/{project_id}/bugs")
        except httpx.HTTPError as exc:
            logger.warning("Replay bug list for %s failed: %s", project_id, exc)
            return None
        if response.status_code >= 400:
            logger.warning(
                "Replay %s listing bugs for %s: %s",
                response.status_code,
                project_id,
                response.text[:200],
            )
            return None

        try:
            body = response.json()
        except ValueError:
            logger.warning("Replay bug list for %s was not JSON.", project_id)
            return None
        # The response schema is undocumented, so accept both a bare list and the common
        # envelope spellings instead of assuming one.
        if isinstance(body, list):
            return [b for b in body if isinstance(b, dict)]
        for key in ("bugs", "data", "items", "results"):
            value = body.get(key) if isinstance(body, dict) else None
            if isinstance(value, list):
                return [b for b in value if isinstance(b, dict)]
        # `None`, not `[]`: an unrecognised envelope is a parsing failure on our side, and
        # reporting it as an empty result would let the caller conclude the app is clean.
        logger.warning("Replay bug list had an unrecognised shape: %r", type(body).__name__)
        return None

    def _to_finding(self, bug: dict[str, Any], scan_id: str) -> RawFinding:
        title = str(bug.get("title") or "Untitled finding")
        return RawFinding(
            scan_id=scan_id,
            # Replay organises by journey; the bug payload does not carry a journey name, so the
            # polish category is the closest honest grouping.
            journey=str(bug.get("polish_category") or "exploration"),
            step_intent=_as_text(bug.get("reproduction_steps")) or title,
            expected=_as_text(bug.get("expected_behavior")) or "The app behaves as a user expects.",
            observed=_as_text(bug.get("actual_behavior") or bug.get("description")) or title,
            source="replay",
            agent_severity=_SEVERITY_MAP.get(
                str(bug.get("severity") or "").strip().lower(), "minor"
            ),
            # Replay reports no confidence score. 0.6 keeps these mid-pack in the v1 ranking,
            # which is the honest position for a finding whose confidence we do not know — and
            # round 1 is precisely what replaces this guess with human labels.
            agent_confidence=0.6,
            category=str(bug.get("polish_category") or "uncategorized"),
        )
