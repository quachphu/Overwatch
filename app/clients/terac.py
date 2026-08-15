"""Terac REST v2 client.

There is no Terac SDK. This is hand-rolled against the live OpenAPI 3.0.3 spec at
<https://terac.com/api/external/v2/openapi.json>, vendored to `docs/terac_openapi.json`.

**Every field name in this file was read from that spec or from the guides.** Nothing is
inferred except where an `# UNKNOWN:` comment says so. See RESEARCH.md §1 for the full
transcription and §2 for the constraints that produce a 4xx.

Guides referenced inline:
  https://terac.com/docs/developers/guides/authentication
  https://terac.com/docs/developers/guides/errors
  https://terac.com/docs/developers/guides/filters
  https://terac.com/docs/developers/guides/filters/catalog
  https://terac.com/docs/developers/guides/screening-questions
  https://terac.com/docs/developers/guides/quotas
  https://terac.com/docs/developers/guides/webhooks
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Iterable
from typing import Any, Literal

import httpx

from app.config import settings
from app.models import (
    TeracOpportunity,
    TeracOrgContext,
    TeracQuote,
    TeracSubmission,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════════════
# Constants that depend on open questions. Each is a one-line edit. RESEARCH.md §9.
# ══════════════════════════════════════════════════════════════════════════════════════

# UNKNOWN 1: the spec enforces `expected_days_to_complete` minimum 5 (docs/terac_openapi.json,
# properties.expected_days_to_complete.minimum). Whether hackathon keys waive it is not
# documented. Sending the documented floor.
EXPECTED_DAYS = 5

# UNKNOWN 11: `task_type` enum is ["interview", "file_upload", "activity"] — there is NO
# "survey" value, which is what SPECS.md §5.2 originally specified and would have 400'd.
# "activity" is the only member that plausibly means "open this URL and do the thing";
# `interview` implies a live session and `file_upload` implies an artifact. This is an
# INFERENCE from the enum, not a documented mapping. Highest-value question for the booth.
TASK_TYPE: Literal["interview", "file_upload", "activity"] = "activity"

# docs/DECISIONS.md 005: auto_approve, because manual review makes a human the bottleneck.
REVIEW_TYPE: Literal["auto_approve", "manual_review", "self_report"] = "auto_approve"

# UNKNOWN 5: `reference--has_not_taken_study` is confirmed to exist in the filter catalog
# ("Has NOT completed specific study(s)") but its operators are not published there, and
# the spec types filter values loosely as number|string|string[]. `$in` with a list is the
# shape every other multi-value filter uses. Run `scripts/probe_terac.py --filters` to read
# the real `operators[]` for this slug from GET /filters before Round 2 launches.
ROUND2_EXCLUSION_OPERATOR = "$in"
ROUND2_EXCLUSION_SLUG = "reference--has_not_taken_study"

# Rate limit is 100 req/min per key (authentication guide). 90 leaves headroom for the
# polling loop and the agents sharing one key.
RATE_LIMIT_PER_MINUTE = 90


class TeracError(RuntimeError):
    """A Terac API error, with the parsed error envelope attached.

    Envelope shape from https://terac.com/docs/developers/guides/errors:
        {"error": {"code", "message", "details": [{"field", "message"}]}}
    """

    def __init__(self, status: int, body: Any, *, method: str = "", url: str = "") -> None:
        self.status = status
        self.body = body
        envelope = body.get("error") if isinstance(body, dict) else None
        self.code = (envelope or {}).get("code") if isinstance(envelope, dict) else None
        self.message = (envelope or {}).get("message") if isinstance(envelope, dict) else str(body)
        self.details = (envelope or {}).get("details") or [] if isinstance(envelope, dict) else []

        detail_text = "; ".join(
            f"{d.get('field')}: {d.get('message')}" for d in self.details if isinstance(d, dict)
        )
        super().__init__(
            f"Terac {status} {self.code or ''} on {method} {url}: {self.message}"
            + (f" [{detail_text}]" if detail_text else "")
        )


class _RateLimiter:
    """Naive sliding-window limiter. 100/min is a hard ceiling we must not test."""

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        self._hits: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._hits = [t for t in self._hits if now - t < 60.0]
            if len(self._hits) >= self._per_minute:
                sleep_for = 60.0 - (now - self._hits[0]) + 0.05
                logger.warning("Terac rate limit guard: sleeping %.1fs", sleep_for)
                await asyncio.sleep(max(sleep_for, 0.0))
                self._hits = [t for t in self._hits if time.monotonic() - t < 60.0]
            self._hits.append(time.monotonic())


class TeracClient:
    """Async client. One instance per process; safe to share."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or settings.terac_api_key
        if not self.api_key:
            raise RuntimeError(
                "TERAC_API_KEY is not set. It comes in the attendee doc at the 09:15 "
                "opening ceremony. See .env.example."
            )
        self.base_url = (base_url or settings.terac_base_url).rstrip("/")
        self._timeout = timeout
        self._limiter = _RateLimiter(RATE_LIMIT_PER_MINUTE)
        self._client: httpx.AsyncClient | None = None

    # ── plumbing ─────────────────────────────────────────────────────────────────────

    @property
    def _headers(self) -> dict[str, str]:
        # Auth per https://terac.com/docs/developers/guides/authentication
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def __aenter__(self) -> TeracClient:
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=self._headers, timeout=self._timeout
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retries: int = 3,
        ok_statuses: tuple[int, ...] = (),
    ) -> Any:
        """Issue one request, with retry on 429 and 5xx only.

        Note `json_body` defaults to `{}` on POST rather than `None`: every POST on this
        API expects a JSON body and a Content-Type even when the endpoint takes only a path
        parameter, and a bodyless POST returns 415.
        docs: https://terac.com/docs/developers/guides/webhooks
        """
        if method == "POST" and json_body is None:
            json_body = {}

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=self.base_url, headers=self._headers, timeout=self._timeout
        )

        try:
            last_error: TeracError | None = None
            for attempt in range(retries):
                await self._limiter.acquire()
                try:
                    response = await client.request(method, path, json=json_body, params=params)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt == retries - 1:
                        raise TeracError(
                            0, {"error": {"message": str(exc)}}, method=method, url=path
                        ) from exc
                    await asyncio.sleep(2**attempt + random.random())
                    continue

                if response.status_code in ok_statuses:
                    return self._parse(response)

                if response.status_code < 400:
                    return self._parse(response)

                body = self._parse(response, quiet=True)
                error = TeracError(response.status_code, body, method=method, url=path)

                # 429 and 5xx are transient. Everything else is our payload being wrong,
                # and retrying an identical bad request just burns the rate limit.
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = error
                    if attempt < retries - 1:
                        backoff = 2**attempt + random.random()
                        logger.warning(
                            "Terac %s on %s %s, retrying in %.1fs",
                            response.status_code,
                            method,
                            path,
                            backoff,
                        )
                        await asyncio.sleep(backoff)
                        continue
                raise error

            raise last_error or TeracError(0, {}, method=method, url=path)
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _parse(response: httpx.Response, *, quiet: bool = False) -> Any:
        try:
            return response.json()
        except Exception:
            if not quiet:
                logger.warning("Non-JSON response from Terac: %s", response.text[:400])
            return {"error": {"message": response.text[:1000]}}

    # ── projects ─────────────────────────────────────────────────────────────────────

    async def create_project(self, name: str) -> dict[str, Any]:
        """`POST /projects`."""
        return await self._request("POST", "/projects", json_body={"name": name})

    async def ensure_project(self, name: str) -> str:
        """Return a usable project id, reusing the configured one when present."""
        if settings.terac_project_id:
            return settings.terac_project_id
        created = await self.create_project(name)
        project_id = created.get("id")
        if not project_id:
            raise TeracError(0, created, method="POST", url="/projects")
        return str(project_id)

    # ── opportunities ────────────────────────────────────────────────────────────────

    async def create_opportunity(self, payload: dict[str, Any]) -> TeracOpportunity:
        """`POST /opportunities` — creates a **draft**. Launching is separate."""
        body = await self._request("POST", "/opportunities", json_body=payload)
        return TeracOpportunity.model_validate(body)

    async def launch_opportunity(self, opportunity_id: str) -> dict[str, Any]:
        """`POST /opportunities/{id}/launch`.

        409 CONFLICT means it is already active
        (https://terac.com/docs/developers/guides/errors). Treated as success so that a
        retry after a timeout is idempotent rather than a hard failure at 16:00.
        """
        try:
            return await self._request(
                "POST", f"/opportunities/{opportunity_id}/launch", json_body={}
            )
        except TeracError as exc:
            if exc.status == 409:
                logger.info(
                    "Opportunity %s already active (409) — treating as launched.", opportunity_id
                )
                return {"id": opportunity_id, "status": "active", "already_active": True}
            raise

    async def get_opportunity(self, opportunity_id: str) -> dict[str, Any]:
        """`GET /opportunities/{id}`. Carries `screening_stats` once live."""
        return await self._request("GET", f"/opportunities/{opportunity_id}")

    async def update_opportunity(
        self, opportunity_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        """`PATCH /opportunities/{id}`.

        Confirmed live (2026-08-15): a launched opportunity returns
        `409 CONFLICT "Only draft opportunities can be updated"`. Not documented in the spec —
        only callable on a draft, before `launch_opportunity`. RESEARCH.md §13.25.
        """
        return await self._request("PATCH", f"/opportunities/{opportunity_id}", json_body=patch)

    async def stop_opportunity(self, opportunity_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/opportunities/{opportunity_id}/stop", json_body={})

    # ── submissions ──────────────────────────────────────────────────────────────────

    async def list_submissions(
        self,
        opportunity_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[TeracSubmission]:
        """`GET /opportunities/{id}/submissions`, following cursor pagination to the end.

        `limit` max is 100 (spec). `status` must be one of the seven documented values;
        `approved` is the one the polling loop uses.
        """
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if status:
            params["status"] = status

        out: list[TeracSubmission] = []
        cursor: str | None = None
        while True:
            if cursor:
                params["cursor"] = cursor
            body = await self._request(
                "GET", f"/opportunities/{opportunity_id}/submissions", params=dict(params)
            )
            for row in body.get("data") or []:
                try:
                    out.append(TeracSubmission.model_validate(row))
                except Exception as exc:  # a shape change must not kill the poll loop
                    logger.warning("Unparseable submission %s: %s", row, exc)
            pagination = body.get("pagination") or {}
            if not pagination.get("has_more"):
                break
            cursor = pagination.get("next_cursor")
            if not cursor:
                break
        return out

    async def get_submission(self, submission_id: str) -> TeracSubmission:
        body = await self._request("GET", f"/submissions/{submission_id}")
        return TeracSubmission.model_validate(body)

    async def approve_submission(self, submission_id: str) -> dict[str, Any]:
        """`POST /submissions/{id}/approve`. Approval is the billing event."""
        return await self._request("POST", f"/submissions/{submission_id}/approve", json_body={})

    # ── discovery / accounting ───────────────────────────────────────────────────────

    async def org_context(self) -> TeracOrgContext:
        """`GET /organizations/current/context`.

        Returns `balanceDollars`, which is how we size `num_participants` from the real
        budget instead of asking at a booth. RESEARCH.md §9 UNKNOWN 3.
        """
        body = await self._request("GET", "/organizations/current/context")
        return TeracOrgContext.model_validate(body)

    async def list_filters(self) -> list[dict[str, Any]]:
        """`GET /filters`. Each entry carries `operators[]`, and `options_url` on selects.

        This is the runtime answer to UNKNOWN 5: read the real operators for
        `reference--has_not_taken_study` rather than guessing at the value format.
        """
        body = await self._request("GET", "/filters")
        return list(body.get("data") or [])

    async def quote(
        self,
        *,
        task_description: str,
        panel_description: str,
        submission_count: int,
        timeline_hours: int = 72,
    ) -> TeracQuote:
        """`POST /quotes` — synchronous price estimate.

        `timelineHours` minimum is 72 and `submissionCount` max is 999 (spec). Use this
        rather than `/feasibility/requests`, which is human-priced out of band and returns
        `costPerParticipant: null` until someone prices it. RESEARCH.md §1.9.
        """
        body = await self._request(
            "POST",
            "/quotes",
            json_body={
                "taskDescription": task_description,
                "panelDescription": panel_description,
                "timelineHours": max(72, timeline_hours),
                "submissionCount": max(1, min(submission_count, 999)),
            },
        )
        return TeracQuote.model_validate(body)

    # ── webhooks ─────────────────────────────────────────────────────────────────────

    async def create_webhook_subscription(
        self, target_url: str, event_types: Iterable[str] = ("submission.approved",)
    ) -> dict[str, Any]:
        """`POST /hooks/subscriptions`.

        Returns the signing `secret` (`whsec_…`) and `confirmed_at: null`. The
        subscription receives **nothing** until confirmed — see `confirm_webhook_subscription`.

        docs/DECISIONS.md and the webhooks guide both warn against subscribing to
        `submission.status.change` as well: a submission emits roughly five status changes,
        so taking both means every approval arrives twice with a different `X-Event-ID`.
        """
        return await self._request(
            "POST",
            "/hooks/subscriptions",
            json_body={"target_url": target_url, "event_types": list(event_types)},
        )

    async def confirm_webhook_subscription(self, subscription_id: str) -> dict[str, Any]:
        """`POST /hooks/subscriptions/{id}` with `{}`.

        Terac POSTs one signed `webhook.ping` to `target_url`. Answer 2xx and the
        subscription starts receiving events; anything else returns 412 and nothing is
        confirmed. This is also the end-to-end test of our signature verification, because
        the ping carries the same headers as a real delivery.
        """
        return await self._request("POST", f"/hooks/subscriptions/{subscription_id}", json_body={})

    async def get_webhook_secret(self, subscription_id: str) -> dict[str, Any]:
        """`GET /hooks/subscriptions/{id}/secret` — recover a secret without rotating."""
        return await self._request("GET", f"/hooks/subscriptions/{subscription_id}/secret")

    async def list_event_types(self) -> list[dict[str, Any]]:
        """`GET /hooks/event-types`. Read rather than hardcode; new types appear here first."""
        body = await self._request("GET", "/hooks/event-types")
        return list(body.get("data") or [])

    async def ensure_webhook(self, target_url: str) -> dict[str, Any]:
        """Create-then-confirm in one call, tolerating the already-registered case.

        A second POST for a URL already registered returns 409 naming the existing
        subscription, so that is a success for our purposes.
        """
        try:
            created = await self.create_webhook_subscription(target_url)
        except TeracError as exc:
            if exc.status == 409:
                logger.info("Webhook for %s already registered (409).", target_url)
                existing = await self._request("GET", "/hooks/subscriptions")
                for row in existing.get("data") or []:
                    if row.get("target_url") == target_url:
                        return row
                return {"target_url": target_url, "already_registered": True}
            raise

        subscription_id = created.get("id")
        if subscription_id:
            try:
                await self.confirm_webhook_subscription(str(subscription_id))
                created["confirmed"] = True
            except TeracError as exc:
                # 412 means our receiver did not answer 2xx. Polling still covers us
                # (docs/DECISIONS.md 007), so this is a warning, not a failure.
                logger.error(
                    "Webhook confirmation failed for %s: %s. Polling remains the fallback.",
                    subscription_id,
                    exc,
                )
                created["confirmed"] = False
        return created


# ══════════════════════════════════════════════════════════════════════════════════════
# Opportunity payload builders
#
# These are the "constructed from what Triage actually found" part of the delete test
# (docs/AGENTS.md). `num_participants` and the quota targets are derived from the actual
# finding count, so the payload cannot be a stored template.
# ══════════════════════════════════════════════════════════════════════════════════════


def _attention_check() -> dict[str, Any]:
    """A screener that is cheap for an honest reader and expensive for a bot.

    The qualifying answer is deliberately not the first option: the screening-questions
    guide advises designing these "so the qualifying answer is not obvious".
    """
    return {
        "key": "attn",
        "text": "What will you be looking at in this task?",
        "pick": "one",
        "answers": [
            {"text": "Two audio clips", "qualify_logic": "reject"},
            {"text": "Two screenshots of a website", "qualify_logic": "must"},
            {"text": "A printed paper receipt", "qualify_logic": "reject"},
        ],
    }


def _device_question() -> dict[str, Any]:
    return {
        "key": "device",
        "text": "What are you using right now?",
        "pick": "one",
        "answers": [
            {"text": "Laptop or desktop", "qualify_logic": "may"},
            {"text": "Phone or tablet", "qualify_logic": "may"},
        ],
    }


def build_round1_payload(
    *,
    project_id: str,
    task_url: str,
    num_participants: int,
    n_findings: int,
    findings_per_participant: int,
    business_type: Literal["b2c", "b2b"] = "b2c",
    duration_minutes: int = 3,
) -> dict[str, Any]:
    """Round 1 — verification. SPECS.md §5.2, corrected against the spec.

    Differences from the payload in SPECS.md, all deliberate:

    * `task_type` is `activity`, because `survey` is not a member of the enum and would
      have returned 400 on every call (RESEARCH.md §10.1).
    * The device quota is a **desktop-only minimum**. SPECS.md set minimums on both
      desktop (6) and mobile (4); with two interlocked floors and unknown fill latency, a
      slow mobile cell stalls the round we cannot afford to be late (RESEARCH.md §10.8).
    """
    desktop_target = max(1, num_participants // 2)

    return {
        "title": "Did this web app actually fail? (3 min, screenshots only)",
        "internal_title": f"Overwatch R1 · {n_findings} findings",
        "description": (
            "Overwatch is an automated QA tool: a headless browser crawls a public website, "
            "clicking around and watching for console errors, failed network requests, and "
            "server error pages. No human has looked at what it found yet — that's this "
            "study. You'll be shown the evidence for a handful of things it flagged (what it "
            "tried to do, what it expected, what it actually saw, and a before/after "
            "screenshot) and asked whether each one looks like a genuine problem and how bad "
            "it would be if it were real. Your answers directly change the report we hand "
            "back to the site's owner — findings the group confirms move up, findings the "
            "group doesn't buy get demoted or cut. You need no technical background: if the "
            "screenshots don't look broken to you, saying so is a useful answer. No account, "
            "no downloads, and you never visit or interact with the site yourself — "
            f"everything you need is on the task page. {findings_per_participant} cases, "
            "about 3 minutes."
        ),
        "project_id": project_id,
        "num_participants": num_participants,
        "business_type": business_type,
        "expected_days_to_complete": EXPECTED_DAYS,
        "filters": [
            {"multi_select--country": {"$in": ["US"]}},
            {"integer--age": {"$gte": 18, "$lte": 65}},
            # docs (live 400, 2026-08-15): the allowed set is full locale codes
            # (en-US, es-ES, ...), not bare "en" — RESEARCH.md §13.16.
            {"multi_select--language": {"$in": ["en-US"]}},
        ],
        "screening_questions": [_attention_check(), _device_question()],
        "cross_quotas": [
            {
                "label": "Desktop",
                "conditions": [{"screening_question": "device", "answer": "Laptop or desktop"}],
                "target": desktop_target,
                "quota_type": "minimum",
            }
        ],
        "tasks": [
            {
                "sequence": 1,
                "task_type": TASK_TYPE,
                "review_type": REVIEW_TYPE,
                "task_url": task_url,
                "title": "Judge automated bug findings from screenshots",
                "description": (
                    "For each case on the page you'll see four things: what our test tried "
                    "to do, what it expected to happen, what it actually observed (console "
                    "errors, failed requests, or an error status code), and a screenshot from "
                    "before and after the action. Read those, then answer two questions per "
                    "case — does this look like a real problem, and if so how severe — based "
                    "only on what's shown. There's no trick answer and no penalty for saying "
                    "a flagged case looks fine to you; that's a real, useful result too."
                ),
                "duration_minutes": duration_minutes,
            }
        ],
    }


def build_round2_payload(
    *,
    project_id: str,
    task_url: str,
    num_participants: int,
    round1_opportunity_id: str | None,
    business_type: Literal["b2c", "b2b"] = "b2c",
    duration_minutes: int = 3,
) -> dict[str, Any]:
    """Round 2 — fresh judging. SPECS.md §5.3.

    The `reference--has_not_taken_study` filter is what makes "a fresh panel" true rather
    than a claim. `docs/AGENTS.md` requires Recruiter to **block rather than launch** if it
    cannot be constructed, and `build_round2_payload` refuses to build a payload without
    the round-1 opportunity id for exactly that reason.
    """
    if not round1_opportunity_id:
        raise ValueError(
            "BLOCKED: cannot exclude round 1 participants — no round-1 opportunity id. "
            "Launching without the has_not_taken_study filter would make the 'fresh panel' "
            "claim false (docs/DECISIONS.md 006)."
        )

    return {
        "title": "Which bug report is more useful? (3 min, side by side)",
        "internal_title": "Overwatch R2 · v1 vs v2",
        "description": (
            "Overwatch is an automated QA tool that scans public websites for bugs, then has "
            "real people verify which findings are genuine and how severe they are — a "
            "separate group did that verification step for the website in this study, "
            "earlier. You are a completely fresh panel: you have not seen this website's "
            "report before, and you are not told which list below reflects that earlier "
            "human input. You'll see two ranked bug reports side by side, describing the same "
            "automated test of the same website, just in a different order. Pick the one "
            "that would be more useful to a team that only has time to fix problems from the "
            "top of the list down, and say why in one line. Your pick is the actual "
            "measurement of whether that earlier round of human verification made the report "
            "better — not a guess, a number we report either way."
        ),
        "project_id": project_id,
        "num_participants": num_participants,
        "business_type": business_type,
        "expected_days_to_complete": EXPECTED_DAYS,
        "filters": [
            {"multi_select--country": {"$in": ["US"]}},
            {"integer--age": {"$gte": 18, "$lte": 65}},
            # docs (live 400, 2026-08-15): the allowed set is full locale codes
            # (en-US, es-ES, ...), not bare "en" — RESEARCH.md §13.16.
            {"multi_select--language": {"$in": ["en-US"]}},
            # UNKNOWN 5: operator and value format for reference-- filters are not
            # published. `$in` with a list of opportunity ids is the shape every other
            # multi-value filter uses. Verify with `scripts/probe_terac.py --filters`.
            {ROUND2_EXCLUSION_SLUG: {ROUND2_EXCLUSION_OPERATOR: [round1_opportunity_id]}},
        ],
        "screening_questions": [
            {
                "key": "attn2",
                "text": "What are you comparing in this task?",
                "pick": "one",
                "answers": [
                    {"text": "Two recipes", "qualify_logic": "reject"},
                    {"text": "Two bug reports for a website", "qualify_logic": "must"},
                    {"text": "Two job applications", "qualify_logic": "reject"},
                ],
            }
        ],
        "tasks": [
            {
                "sequence": 1,
                "task_type": TASK_TYPE,
                "review_type": REVIEW_TYPE,
                "task_url": task_url,
                "title": "Compare two bug reports",
                "description": (
                    "Report A and Report B below list the same problems, found by the same "
                    "automated test of the same website — just ordered differently. Read down "
                    "each list the way you would if you were the one fixing this site and "
                    "only had time to work from the top. Choose whichever one you'd rather "
                    "receive, then add a one-line reason (optional but useful — e.g. "
                    "'the serious problems are nearer the top')."
                ),
                "duration_minutes": duration_minutes,
            }
        ],
    }


def participant_task_url(base_url: str, path: str) -> str:
    """Build a `task_url` carrying Terac's participant placeholder.

    SPECS.md §5.5: without a real participant id in the URL there is no way to join a Terac
    submission to the labels it produced, and the dataset is unusable. The task page
    persists it before rendering anything.

    UNKNOWN 12 resolved live and the wrong way, 2026-08-15 (RESEARCH.md §13.26): this used to
    emit `{{participant_id}}` (double-brace), inferred rather than confirmed. A real round-1
    participant's actual request came back as `...?pid=%7B%7Bparticipant_id%7D%7D` — Terac
    never substituted it — while the *same* URL's `{submissionId}` and `{taskId}` (single-brace,
    read back verbatim on `participant_url_template`) were substituted correctly. Terac's
    placeholder syntax is single-brace, matching every other field it substitutes.
    """
    return f"{base_url.rstrip('/')}{path}?pid={{participant_id}}"
