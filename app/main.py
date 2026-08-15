"""FastAPI ingress: scan intake, webhooks, participant task pages, dashboard, report.

Server-rendered Jinja throughout (docs/DECISIONS.md 009) — no build step on the path that
serves Terac participants at 16:00. The React landing page in `front-end/` is built and
served as static files, so a broken node toolchain can never take down a task page.

Route groups:

* `/` `/api/scan`                      customer-facing ingress — scanning is free, no paywall
* `/hooks/terac`                       webhook receiver, HMAC-verified, ACK-then-work
* `/t/r1/{scan_id}` `/t/r2/{scan_id}`   Terac participant task pages
* `/t/smoke`                            the 10:45 latency probe's landing page
* `/report/{scan_id}` `/dashboard`      deliverables
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select, update

from app import pipeline
from app.clients.terac import TeracClient
from app.config import settings
from app.db import init_db, session_scope
from app.models import (
    Assignment,
    Finding,
    HumanLabel,
    Label,
    Preference,
    Round,
    Scan,
    WebhookEvent,
)
from app.security import (
    UnsafeTargetError,
    verify_terac_signature,
)
from app.sources.evidence import evidence_root, public_url, to_display_path

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Create the schema and the evidence directory before serving.

    A lifespan handler rather than `@app.on_event("startup")`, which FastAPI has deprecated.

    The warning about a publicly reachable instance with no operator token is emitted here so it
    lands in the deploy log at boot, rather than being discovered when someone finds the
    money-spending endpoints returning 503.
    """
    init_db()
    evidence_root()
    logger.info("Overwatch up. Public base: %s", settings.public_base_url)
    if settings.is_publicly_reachable and not settings.operator_token:
        logger.warning(
            "OPERATOR_TOKEN is not set but %s looks publicly reachable. The endpoints that "
            "spend Terac credit will refuse with 503 until it is set.",
            settings.public_base_url,
        )
    yield


app = FastAPI(
    title="Overwatch",
    description="Agent-run QA with verified human judgment.",
    lifespan=lifespan,
)


# ══════════════════════════════════════════════════════════════════════════════════════
# Operator authentication
# ══════════════════════════════════════════════════════════════════════════════════════


def require_operator(
    authorization: str | None = Header(default=None),
    x_operator_token: str | None = Header(default=None),
) -> None:
    """Guard the endpoints that spend money.

    `POST /api/scans/{id}/round1` hires twelve people. It was reachable by anyone who could
    guess a scan id, and it is idempotent in neither direction — each call creates a fresh
    opportunity, so a loop over it is a loop over our Terac balance.

    The rule is keyed to reachability rather than to a debug flag:

    * public host (Render, a tunnel) -> a token is **mandatory**. If `OPERATOR_TOKEN` is unset
      the endpoint returns 503 rather than running, because failing open on a public host is
      how a balance disappears.
    * localhost -> allowed without a token, so `make rehearse` and the demo stay frictionless.

    Accepts `Authorization: Bearer <token>` or `X-Operator-Token`. Compared with
    `secrets.compare_digest` — a plain `==` leaks the token a byte at a time under timing
    analysis, and this one authorises spending.
    """
    expected = settings.operator_token

    if not expected:
        if settings.is_publicly_reachable:
            raise HTTPException(
                status_code=503,
                detail=(
                    "OPERATOR_TOKEN is not set and this instance is publicly reachable at "
                    f"{settings.public_base_url}. Refusing to expose endpoints that spend "
                    "money. Set OPERATOR_TOKEN (see .env.example)."
                ),
            )
        return  # localhost development

    presented = x_operator_token
    if not presented and authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()

    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Operator token required.")


# Evidence must be publicly fetchable — Terac participants load these from their phones.
app.mount("/evidence", StaticFiles(directory=str(evidence_root())), name="evidence")

# The built React landing page, if it exists. Mounted at /app so it cannot shadow API routes.
_frontend_dist = BASE_DIR.parent / "front-end" / "dist"
if _frontend_dist.exists():
    app.mount("/app", StaticFiles(directory=str(_frontend_dist), html=True), name="landing")
    # `/` returns dist/index.html directly, and Vite builds it with relative asset URLs, so
    # those resolve to /assets/* at the root. Without this mount the landing page loads as
    # unstyled HTML with no JS.
    _frontend_assets = _frontend_dist / "assets"
    if _frontend_assets.exists():
        app.mount("/assets", StaticFiles(directory=str(_frontend_assets)), name="landing-assets")

_static_dir = BASE_DIR / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ══════════════════════════════════════════════════════════════════════════════════════
# Health and landing
# ══════════════════════════════════════════════════════════════════════════════════════


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Kept trivial and dependency-free: Render's health check must not touch the DB."""
    return {"ok": True, "service": "overwatch", "time": datetime.now(UTC).isoformat()}


@app.get("/", response_class=HTMLResponse)
def landing(request: Request) -> Any:
    """The marketing hero.

    Served from the built React app when present, otherwise from the Jinja mirror of the
    same design. Either way `/` always renders — a missing node build is not an outage.
    """
    index = _frontend_dist / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return templates.TemplateResponse(request, "landing.html", {"settings": settings})


# ══════════════════════════════════════════════════════════════════════════════════════
# Ingress
# ══════════════════════════════════════════════════════════════════════════════════════


@app.post("/api/scan")
async def api_scan(
    request: Request,
    background: BackgroundTasks,
) -> JSONResponse:
    """Start a scan for a pasted URL.

    Runs the scan in the background and returns immediately: a Playwright journey takes
    minutes and an HTTP request that holds open for minutes is a request that times out
    behind a proxy.
    """
    payload = await _json_or_form(request)
    url = str(payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="A url is required.")

    try:
        scan_id = pipeline.create_scan(url, order_id=payload.get("order_id"))
    except UnsafeTargetError as exc:
        # SPECS.md §9 — refusing a private/loopback target is a 400, and the reason is
        # returned so the caller learns why rather than seeing a generic failure.
        raise HTTPException(status_code=400, detail=f"Refusing to scan this target: {exc}") from exc

    background.add_task(_run_scan_safely, scan_id)

    return JSONResponse(
        {
            "scan_id": scan_id,
            "status": "scanning",
            "report_url": f"{settings.public_base_url}/report/{scan_id}",
            "dashboard_url": f"{settings.public_base_url}/dashboard?scan_id={scan_id}",
        }
    )


async def _run_scan_safely(scan_id: str) -> None:
    try:
        await pipeline.run_scan(scan_id)
    except Exception:
        logger.exception("Background scan %s failed", scan_id)




# ══════════════════════════════════════════════════════════════════════════════════════
# Webhooks — ACK first, work after
# ══════════════════════════════════════════════════════════════════════════════════════


@app.post("/hooks/terac")
async def hooks_terac(
    request: Request,
    background: BackgroundTasks,
    x_terac_request_signature: str | None = Header(default=None),
    x_terac_request_timestamp: str | None = Header(default=None),
    x_event_id: str | None = Header(default=None),
    x_timestamp: str | None = Header(default=None),
) -> JSONResponse:
    """Terac webhook receiver.

    Four constraints from https://terac.com/docs/developers/guides/webhooks, all load-bearing:

    * Signature is over `timestamp + RAW BODY`. We read `await request.body()` and never
      re-serialize — parsing and re-encoding changes the bytes and the HMAC will not match.
    * Deduplicate on `X-Event-ID`, stable across retries.
    * Deliveries time out after 10s, so ACK `2xx` first and do the work afterwards.
    * A non-2xx that is not 5xx/408/429 is treated as a deliberate rejection and never
      retried, so we must not return 4xx for a transient problem of our own.
    """
    raw = await request.body()

    secret = settings.terac_webhook_secret
    if secret:
        if not verify_terac_signature(
            raw, x_terac_request_signature, x_terac_request_timestamp, secret
        ):
            logger.warning("Rejected Terac webhook with a bad signature.")
            raise HTTPException(status_code=401, detail="Invalid signature.")
    else:
        # Refusing here would break the confirmation ping and leave the subscription
        # unconfirmed and silent. Loud log, accepted delivery, polling still covers us.
        logger.error(
            "TERAC_WEBHOOK_SECRET is not set — accepting webhook UNVERIFIED. Set it before "
            "the demo."
        )

    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        logger.warning("Terac webhook body was not JSON: %r", raw[:200])
        return JSONResponse({"ok": True, "ignored": "non-json"})

    event_id = x_event_id or body.get("event_id")
    event_type = body.get("event_type", "")

    # The confirmation handshake. Answering 2xx here is what activates the subscription.
    if event_type == "webhook.ping":
        logger.info("Terac webhook ping received and verified.")
        return JSONResponse({"ok": True, "pong": True})

    if event_id:
        with session_scope() as session:
            if session.get(WebhookEvent, event_id) is not None:
                logger.info("Duplicate Terac delivery %s ignored.", event_id)
                return JSONResponse({"ok": True, "duplicate": True})
            session.add(
                WebhookEvent(
                    event_id=event_id,
                    provider="terac",
                    event_type=event_type,
                    resource_id=body.get("resource_id"),
                    payload=body,
                )
            )

    background.add_task(_process_terac_event, body)
    return JSONResponse({"ok": True})


async def _process_terac_event(body: dict[str, Any]) -> None:
    """Handle an approved submission after the ACK has gone out."""
    if body.get("event_type") != "submission.approved" and body.get("to") != "approved":
        return

    submission_id = body.get("resource_id")
    opportunity_id = body.get("opportunity_id")
    if not opportunity_id:
        return

    with session_scope() as session:
        round_row = session.scalars(
            select(Round).where(Round.opportunity_id == str(opportunity_id))
        ).first()
        if round_row is None:
            logger.info("Approved submission for unknown opportunity %s", opportunity_id)
            return
        scan_id, round_no = round_row.scan_id, round_row.round_no
        if round_row.first_completion_at is None:
            round_row.first_completion_at = datetime.now(UTC)
            elapsed = (
                (round_row.first_completion_at - round_row.launched_at).total_seconds() / 60
                if round_row.launched_at
                else None
            )
            # The number the whole schedule depends on (RESEARCH.md §8.4).
            logger.info(
                "FIRST COMPLETION for round %s of scan %s at +%s minutes.",
                round_no,
                scan_id,
                f"{elapsed:.1f}" if elapsed is not None else "unknown",
            )

    logger.info("Submission %s approved for scan %s round %s", submission_id, scan_id, round_no)

    # Round 1 finishing is what unlocks v2. Rebuild on each approval so v2 is always
    # current and round 2 can launch on partial labels at 16:00 if it has to.
    if round_no == 1:
        try:
            pipeline.build_v2(scan_id)
        except Exception:
            logger.exception("build_v2 failed for %s", scan_id)


# ══════════════════════════════════════════════════════════════════════════════════════
# Participant task pages
# ══════════════════════════════════════════════════════════════════════════════════════


def _claim_slot(scan_id: str, round_no: int, participant_id: str) -> Assignment | None:
    """Bind a participant to an assignment slot, stably and atomically.

    Returning the same slot on a refresh is what makes the round-2 side assignment stable
    (SPECS.md §5.3) and stops a participant re-judging a different finding set.

    The claim is a **conditional UPDATE**, not a read-then-write. Terac participants arrive in a
    burst the moment an opportunity goes live, and selecting the lowest free slot and then
    writing to it lets two simultaneous arrivals select the same slot and both write, last write
    winning. The visible result is not an error: one slot ends up shared by two people while
    another is never claimed, so some findings collect six raters and others none.

    That silently destroys the balanced assignment `triage.assign_round1` exists to guarantee,
    and "majority of 3 raters" is undefined for the findings that lost their raters — after we
    have paid for the judgments.

    `UPDATE ... WHERE participant_id IS NULL` makes exactly one writer win. The loser sees
    `rowcount == 0` and tries the next slot. Bounded by the number of slots, so it terminates.
    """
    with session_scope() as session:
        mine = session.scalars(
            select(Assignment).where(
                Assignment.scan_id == scan_id,
                Assignment.round_no == round_no,
                Assignment.participant_id == participant_id,
            )
        ).first()
        if mine is not None:
            session.expunge(mine)
            return mine

        free_slots = list(
            session.scalars(
                select(Assignment.slot)
                .where(
                    Assignment.scan_id == scan_id,
                    Assignment.round_no == round_no,
                    Assignment.participant_id.is_(None),
                )
                .order_by(Assignment.slot)
            )
        )

        for slot in free_slots:
            claimed = session.execute(
                update(Assignment)
                .where(
                    Assignment.scan_id == scan_id,
                    Assignment.round_no == round_no,
                    Assignment.slot == slot,
                    Assignment.participant_id.is_(None),
                )
                .values(participant_id=participant_id, claimed_at=datetime.now(UTC))
            ).rowcount

            if claimed:
                session.commit()
                row = session.scalars(
                    select(Assignment).where(
                        Assignment.scan_id == scan_id,
                        Assignment.round_no == round_no,
                        Assignment.slot == slot,
                    )
                ).first()
                if row is not None:
                    session.expunge(row)
                return row

            # Another request took this slot between the select and the update. Re-read it so
            # the same participant double-tapping the link gets their existing slot back rather
            # than consuming a second one.
            existing = session.scalars(
                select(Assignment).where(
                    Assignment.scan_id == scan_id,
                    Assignment.round_no == round_no,
                    Assignment.slot == slot,
                    Assignment.participant_id == participant_id,
                )
            ).first()
            if existing is not None:
                session.expunge(existing)
                return existing

        return None


@app.get("/t/smoke", response_class=HTMLResponse)
def task_smoke(request: Request, pid: str | None = None) -> Any:
    """Landing page for the 10:45 latency probe.

    Deliberately trivial: it exists to measure minutes-to-first-completion
    (RESEARCH.md §8.4), so it must not fail for any reason of its own.
    """
    return templates.TemplateResponse(
        request, "t_smoke.html", {"participant_id": pid or "", "settings": settings}
    )


@app.get("/t/r1/{scan_id}", response_class=HTMLResponse)
def task_r1(request: Request, scan_id: str, pid: str | None = None) -> Any:
    """Round 1 — verification.

    SPECS.md §5.5: the participant id must be captured **before rendering anything**. If it
    is missing we render a stop page rather than the task, because an unattributed
    submission cannot be joined to a finding and silently poisons the dataset.

    SPECS.md §6 / docs/DECISIONS.md 003: participants see evidence bundles only. They are
    never given live access to the app under test.
    """
    if not pid:
        return templates.TemplateResponse(request, "t_no_pid.html", {"round": 1}, status_code=400)

    assignment = _claim_slot(scan_id, 1, pid)
    if assignment is None:
        return templates.TemplateResponse(request, "t_full.html", {"round": 1})

    with session_scope() as session:
        rows = session.scalars(
            select(Finding).where(Finding.id.in_(list(assignment.finding_ids or [])))
        ).all()
        by_id = {row.id: row for row in rows}

    ordered = [by_id[fid] for fid in (assignment.finding_ids or []) if fid in by_id]
    findings = [
        {
            "id": f.id,
            "journey": f.journey,
            "step_intent": f.step_intent,
            "expected": f.expected,
            "observed": f.observed,
                "before": to_display_path(f.screenshot_before_url),
                "after": to_display_path(f.screenshot_after_url),
                "console_errors": f.console_errors or [],
                "failed_requests": f.failed_requests or [],
            }
            for f in ordered
        ]

    return templates.TemplateResponse(
        request,
        "t_r1.html",
        {
            "scan_id": scan_id,
            "participant_id": pid,
            "findings": findings,
            "n": len(findings),
        },
    )


@app.post("/t/r1/{scan_id}")
async def task_r1_submit(request: Request, scan_id: str) -> Any:
    """Persist round-1 labels.

    Idempotent per (finding, participant, round) via a unique constraint, so a double
    submit does not become two votes.
    """
    form = await request.form()
    participant_id = str(form.get("participant_id") or "").strip()
    if not participant_id:
        raise HTTPException(status_code=400, detail="Missing participant_id.")

    saved = 0
    rejected: list[str] = []
    with session_scope() as session:
        assignment = session.scalars(
            select(Assignment).where(
                Assignment.scan_id == scan_id,
                Assignment.round_no == 1,
                Assignment.participant_id == participant_id,
            )
        ).first()

        # Without this the loop below simply never runs, `saved` stays 0, and we still render
        # the thank-you page — telling someone we paid that their answers mattered while
        # storing nothing. Reachable three ways: an unknown scan_id, a participant who never
        # claimed a slot, and (before the claim was made atomic) whoever lost the slot race.
        if assignment is None:
            logger.error(
                "Round 1 submission from participant %s on scan %s has no assignment. "
                "Refusing to show a success page for judgments we cannot store.",
                participant_id,
                scan_id,
            )
            return templates.TemplateResponse(
                request,
                "t_error.html",
                {"round": 1, "scan_id": scan_id, "participant_id": participant_id},
                status_code=409,
            )

        for finding_id in list(assignment.finding_ids or []):
            is_real = form.get(f"is_real_{finding_id}")
            severity = form.get(f"severity_{finding_id}")
            if not is_real or not severity:
                continue

            # Validate through the model that already declares the domain, one finding at a
            # time. A bare `int(severity)` raised ValueError inside this `session_scope`, and
            # because the scope rolls back on any exception, one malformed field discarded
            # every good label in the same submission and returned a 500.
            try:
                label = HumanLabel(
                    finding_id=finding_id,
                    participant_id=participant_id,
                    is_real=str(is_real),
                    severity=int(str(severity)),
                    note=str(form.get(f"note_{finding_id}") or "") or None,
                )
            except (ValidationError, ValueError):
                # Skip only the bad row. `is_real` outside the vocabulary would otherwise be
                # stored and then read as "not confirmed" by CONFIRMING_ANSWERS, letting a
                # mangled POST silently suppress a finding.
                logger.warning(
                    "Discarding malformed label for %s from %s: is_real=%r severity=%r",
                    finding_id,
                    participant_id,
                    is_real,
                    severity,
                )
                rejected.append(finding_id)
                continue

            existing = session.scalars(
                select(Label).where(
                    Label.finding_id == finding_id,
                    Label.participant_id == participant_id,
                    Label.round_no == 1,
                )
            ).first()
            if existing is not None:
                continue
            session.add(
                Label(
                    finding_id=label.finding_id,
                    scan_id=scan_id,
                    participant_id=label.participant_id,
                    is_real=label.is_real,
                    severity=label.severity,
                    round_no=1,
                    note=label.note,
                )
            )
            saved += 1

        assignment.submitted_at = datetime.now(UTC)

    logger.info(
        "Round 1: %d labels from participant %s on scan %s (%d rejected)",
        saved,
        participant_id,
        scan_id,
        len(rejected),
    )
    return templates.TemplateResponse(
        request, "t_done.html", {"saved": saved, "round": 1, "rejected": len(rejected)}
    )


@app.get("/t/r2/{scan_id}", response_class=HTMLResponse)
def task_r2(request: Request, scan_id: str, pid: str | None = None) -> Any:
    """Round 2 — forced-choice comparison of v1 vs v2, side randomized per participant."""
    if not pid:
        return templates.TemplateResponse(request, "t_no_pid.html", {"round": 2}, status_code=400)

    assignment = _claim_slot(scan_id, 2, pid)
    if assignment is None:
        return templates.TemplateResponse(request, "t_full.html", {"round": 2})

    left_version = assignment.left_version or 1
    right_version = 2 if left_version == 1 else 1

    left = _report_rows(scan_id, left_version)
    right = _report_rows(scan_id, right_version)

    return templates.TemplateResponse(
        request,
        "t_r2.html",
        {
            "scan_id": scan_id,
            "participant_id": pid,
            "left": left,
            "right": right,
            "left_version": left_version,
            "right_version": right_version,
        },
    )


@app.post("/t/r2/{scan_id}")
async def task_r2_submit(request: Request, scan_id: str) -> Any:
    form = await request.form()
    participant_id = str(form.get("participant_id") or "").strip()
    choice = str(form.get("choice") or "").strip()  # "left" | "right"
    if not participant_id or choice not in {"left", "right"}:
        raise HTTPException(status_code=400, detail="Missing participant_id or choice.")

    with session_scope() as session:
        assignment = session.scalars(
            select(Assignment).where(
                Assignment.scan_id == scan_id,
                Assignment.round_no == 2,
                Assignment.participant_id == participant_id,
            )
        ).first()

        # No assignment, no vote. Without this the Preference was written regardless, so anyone
        # who knew a scan id — and every participant does, it is in their task URL — could POST
        # unlimited votes and move `preference_share_v2`, the headline result.
        if assignment is None:
            logger.error(
                "Round 2 vote from participant %s on scan %s has no assignment; rejecting.",
                participant_id,
                scan_id,
            )
            return templates.TemplateResponse(
                request,
                "t_error.html",
                {"round": 2, "scan_id": scan_id, "participant_id": participant_id},
                status_code=409,
            )

        left_version = assignment.left_version or 1

        # Cross-check the side the browser actually rendered against the stored assignment.
        # Re-deriving "left" at POST time from mutable state silently inverted the vote when
        # the two disagreed, recording a preference for v1 from someone who chose v2.
        rendered = str(form.get("left_version") or "").strip()
        if rendered:
            try:
                if int(rendered) != left_version:
                    logger.error(
                        "Round 2 vote from %s rendered left_version=%s but the assignment says "
                        "%s. Rejecting rather than recording a vote that may be inverted.",
                        participant_id,
                        rendered,
                        left_version,
                    )
                    return templates.TemplateResponse(
                        request,
                        "t_error.html",
                        {"round": 2, "scan_id": scan_id, "participant_id": participant_id},
                        status_code=409,
                    )
            except ValueError:
                raise HTTPException(status_code=400, detail="Malformed left_version.") from None

        right_version = 2 if left_version == 1 else 1
        chosen = left_version if choice == "left" else right_version

        already = session.scalars(
            select(Preference).where(
                Preference.scan_id == scan_id, Preference.participant_id == participant_id
            )
        ).first()
        if already is None:
            session.add(
                Preference(
                    scan_id=scan_id,
                    participant_id=participant_id,
                    chose_version=chosen,
                    left_version=left_version,
                    why=str(form.get("why") or "") or None,
                )
            )
        assignment.submitted_at = datetime.now(UTC)

    return templates.TemplateResponse(request, "t_done.html", {"saved": 1, "round": 2})


def _report_rows(scan_id: str, version: int) -> list[dict[str, Any]]:
    """Render-ready rows for one report version, in ranked order.

    Both sides show the same information at the same depth so the comparison is about
    ranking rather than presentation. Labels say "Report A"/"Report B" in the template — a
    participant must never see which one is the newer version.
    """
    ranked = pipeline.get_report(scan_id, version)
    with session_scope() as session:
        rows = session.scalars(select(Finding).where(Finding.scan_id == scan_id)).all()
        by_id = {row.id: row for row in rows}
    out: list[dict[str, Any]] = []
    for position, fid in enumerate(ranked[:10], start=1):
        finding = by_id.get(fid)
        if finding is None:
            continue
        out.append(
            {
                "position": position,
                "journey": finding.journey,
                "step_intent": finding.step_intent,
                "observed": finding.observed,
                "severity": finding.agent_severity,
            }
        )
    return out


# ══════════════════════════════════════════════════════════════════════════════════════
# Deliverables
# ══════════════════════════════════════════════════════════════════════════════════════


@app.get("/report/{scan_id}", response_class=HTMLResponse)
def report_page(request: Request, scan_id: str, version: int = 2) -> Any:
    with session_scope() as session:
        scan = session.get(Scan, scan_id)
        if scan is None:
            raise HTTPException(status_code=404, detail="Unknown scan.")
        scan_url, scan_status, scan_source = scan.url, scan.status, scan.source
        scan_error = scan.error
        released = scan.released
        # sqlite drops tzinfo on round-trip even though the column is declared
        # `DateTime(timezone=True)` — `scan.created_at` comes back naive, and `now_utc()`
        # writes naive-but-UTC values throughout this codebase, so treat it as UTC explicitly
        # rather than comparing a naive and an aware datetime (raises `TypeError`).
        created_at_utc = scan.created_at.replace(tzinfo=UTC) if scan.created_at.tzinfo is None else scan.created_at
        scan_elapsed_seconds = max(0, int((datetime.now(UTC) - created_at_utc).total_seconds()))
        rows = session.scalars(select(Finding).where(Finding.scan_id == scan_id)).all()
        by_id = {row.id: row for row in rows}

    ranked = pipeline.get_report(scan_id, version) or pipeline.get_report(scan_id, 1)
    effective_version = version if pipeline.get_report(scan_id, version) else 1

    findings = []
    for position, fid in enumerate(ranked, start=1):
        finding = by_id.get(fid)
        if finding is None:
            continue
        findings.append(
            {
                "position": position,
                "id": finding.id,
                "journey": finding.journey,
                "step_intent": finding.step_intent,
                "expected": finding.expected,
                "observed": finding.observed,
                "severity": finding.agent_severity,
                "confidence": finding.agent_confidence,
                "category": finding.category,
                "before": to_display_path(finding.screenshot_before_url),
                "after": to_display_path(finding.screenshot_after_url),
                "console_errors": finding.console_errors or [],
                "failed_requests": finding.failed_requests or [],
            }
        )

    proof_screenshots: list[str] = []
    if not findings and scan_status == "clean":
        # A clean result has no Finding rows, so the pages actually visited would otherwise be
        # invisible — indistinguishable from a scan that never ran. `PlaywrightSource` still
        # writes every before/after screenshot to `evidence/{scan_id}/` even when nothing it
        # saw became a finding (app/sources/playwright_source.py `_shot`); glob those out so
        # "no findings" reads as "we looked and it was clean," not "we have nothing to show you."
        scan_evidence_dir = evidence_root() / scan_id
        if scan_evidence_dir.is_dir():
            names = sorted(p.name for p in scan_evidence_dir.glob("*.png"))[:12]
            proof_screenshots = [to_display_path(public_url(scan_id, n)) for n in names]

    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "scan_id": scan_id,
            "scan_url": scan_url,
            "scan_status": scan_status,
            "proof_screenshots": proof_screenshots,
            # The Playwright journey runs as a `BackgroundTask` and can take up to ~90s
            # (12 steps x ~7s each); the caller is redirected here immediately. Without this,
            # the "no findings" branch below fired for a scan that simply had not finished yet
            # — the page told a customer "a clean app is a real result" about a scan still
            # mid-flight, on every single scan, until the background task happened to finish
            # before they looked.
            "scan_in_progress": scan_status in {"queued", "scanning"},
            "scan_elapsed_seconds": scan_elapsed_seconds,
            "scan_error": scan_error,
            "scan_source": scan_source,
            "released": released,
            "version": effective_version,
            "findings": findings,
            "result": pipeline.results(scan_id),
            "vetoes": pipeline.open_vetoes(scan_id),
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, scan_id: str | None = None) -> Any:
    """The chart. This is what gets screen-recorded at 17:30."""
    with session_scope() as session:
        scans = session.scalars(select(Scan).order_by(Scan.created_at.desc())).all()
        scan_list = [
            {"id": s.id, "url": s.url, "status": s.status, "created_at": s.created_at}
            for s in scans
        ]
        chosen = scan_id or (scan_list[0]["id"] if scan_list else None)

        rounds: list[dict[str, Any]] = []
        n_findings = 0
        if chosen:
            round_rows = session.scalars(
                select(Round).where(Round.scan_id == chosen).order_by(Round.round_no)
            ).all()
            rounds = [
                {
                    "round_no": r.round_no,
                    "opportunity_id": r.opportunity_id,
                    "status": r.status,
                    "num_participants": r.num_participants,
                    "launched_at": r.launched_at,
                    "first_completion_at": r.first_completion_at,
                    "minutes_to_first": (
                        (r.first_completion_at - r.launched_at).total_seconds() / 60
                        if r.first_completion_at and r.launched_at
                        else None
                    ),
                    "excludes_round1": bool(
                        r.round_no == 2
                        and any(
                            "has_not_taken_study" in json.dumps(f)
                            for f in (r.request_payload or {}).get("filters", [])
                        )
                    ),
                }
                for r in round_rows
            ]
            n_findings = len(
                session.scalars(select(Finding).where(Finding.scan_id == chosen)).all()
            )

    result = pipeline.results(chosen) if chosen else None

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "scans": scan_list,
            "scan_id": chosen,
            "rounds": rounds,
            "n_findings": n_findings,
            "result": result,
            "settings": settings,
        },
    )


# ══════════════════════════════════════════════════════════════════════════════════════
# Operator API — what the Band agents and the operator call
# ══════════════════════════════════════════════════════════════════════════════════════


def _operator_error(exc: Exception, what: str) -> HTTPException:
    """Map a pipeline failure to an honest status code.

    Mapping everything to 400 said "your request was bad" when the real cause was usually
    Terac being unreachable, and it logged nothing — so a failed round launch left no
    traceback to debug, only a terse string in a curl response.

    `ValueError` is ours (unknown scan, no findings). `TeracError` and anything else is
    upstream or unexpected, which is a 502, and is logged with its stack.
    """
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, RuntimeError):
        # Deliberate refusals: non-public evidence URLs, missing round-1 opportunity id.
        return HTTPException(status_code=409, detail=str(exc))
    logger.exception("%s failed", what)
    return HTTPException(status_code=502, detail=f"{what} failed: {exc}")


@app.post("/api/scans/{scan_id}/round1", dependencies=[Depends(require_operator)])
async def api_launch_round1(scan_id: str, num_participants: int | None = None) -> Any:
    """Spends real money. Operator-authenticated."""
    try:
        return await pipeline.launch_round1(scan_id, num_participants=num_participants)
    except Exception as exc:
        raise _operator_error(exc, "launch_round1") from exc


@app.post("/api/scans/{scan_id}/v2", dependencies=[Depends(require_operator)])
def api_build_v2(scan_id: str) -> Any:
    try:
        return pipeline.build_v2(scan_id)
    except Exception as exc:
        raise _operator_error(exc, "build_v2") from exc


@app.post("/api/scans/{scan_id}/round2", dependencies=[Depends(require_operator)])
async def api_launch_round2(scan_id: str, num_participants: int | None = None) -> Any:
    """Spends real money. Operator-authenticated."""
    try:
        return await pipeline.launch_round2(scan_id, num_participants=num_participants)
    except Exception as exc:
        raise _operator_error(exc, "launch_round2") from exc


@app.get("/api/scans/{scan_id}/results")
def api_results(scan_id: str) -> Any:
    result = pipeline.results(scan_id)
    return {
        "scan_id": scan_id,
        "n_findings": result.n_findings,
        "n_labels": result.n_labels,
        "n_raters": result.n_raters,
        # Held out: v2 rebuilt without these labels. The only ranking figures that evidence an
        # improvement — see app/metrics.py. Listed first because whatever reads this API first
        # is what ends up quoted.
        "precision_at_10_v1_holdout": result.precision_at_10_v1_holdout,
        "precision_at_10_v2_holdout": result.precision_at_10_v2_holdout,
        "map_v1_holdout": result.map_v1_holdout,
        "map_v2_holdout": result.map_v2_holdout,
        "precision_delta_holdout": result.precision_delta_holdout,
        "holdout_k": result.holdout_k,
        "n_holdout_fit": result.n_holdout_fit,
        "n_holdout_eval": result.n_holdout_eval,
        # In sample: fitted and scored on the same labels. Diagnostic only; rises on noise.
        "precision_at_10_v1": result.precision_at_10_v1,
        "precision_at_10_v2": result.precision_at_10_v2,
        "map_v1": result.map_v1,
        "map_v2": result.map_v2,
        "in_sample_is_circular": True,
        "preference_share_v2": result.preference_share_v2,
        "preference_ci": result.preference_ci,
        "n_preferences": result.n_preferences,
        "preference_is_significant": result.preference_is_significant,
        "confirmation_rate": result.confirmation_rate,
    }


@app.post("/api/scans/{scan_id}/poll", dependencies=[Depends(require_operator)])
async def api_poll(scan_id: str, round_no: int = 1) -> Any:
    """Pull approved submissions directly.

    Operator-authenticated: it spends no credit but it does consume our 100 req/min Terac
    budget and returns participant ids, and neither should be available to strangers.

    docs/DECISIONS.md 007 keeps this alongside webhooks on purpose: webhooks are lower
    latency, polling is debuggable, and at 16:00 there is no time to work out why a single
    delivery went missing.
    """
    with session_scope() as session:
        round_row = session.scalars(
            select(Round).where(Round.scan_id == scan_id, Round.round_no == round_no)
        ).first()
        opportunity_id = round_row.opportunity_id if round_row else None

    if not opportunity_id:
        raise HTTPException(status_code=404, detail="No launched opportunity for that round.")

    async with TeracClient() as terac:
        submissions = await terac.list_submissions(opportunity_id, status="approved")

    counts: dict[str, int] = {}
    for submission in submissions:
        counts[submission.status] = counts.get(submission.status, 0) + 1

    return {
        "opportunity_id": opportunity_id,
        "approved": len(submissions),
        "counts": counts,
        "participant_ids": [s.participant_id for s in submissions],
    }


@app.get("/api/terac/balance", dependencies=[Depends(require_operator)])
async def api_terac_balance() -> Any:
    """Live credit budget. RESEARCH.md §9 UNKNOWN 3, resolved at runtime.

    Operator-authenticated: this is our account balance.
    """
    async with TeracClient() as terac:
        context = await terac.org_context()
    return {
        "organization": context.organizationName,
        "balance_dollars": context.balanceDollars,
        "dashboard": context.dashboard,
    }


async def _json_or_form(request: Request) -> dict[str, Any]:
    """Accept both JSON and form posts, so the landing page and curl both work."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            return dict(await request.json())
        except Exception:
            return {}
    form = await request.form()
    return {k: str(v) for k, v in form.items()}
