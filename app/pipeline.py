"""Scan orchestration.

The durable, synchronous heart of Overwatch. `workflows/scan_workflow.py` wraps these
functions for Render Workflows; the Band agents call the same functions as tools. Neither
path duplicates logic, so the pipeline behaves identically whether it was started by a
paid order, an agent, or a curl.

Every function here is safe to call twice. The human loop takes hours and something will be
retried.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.terac import (
    TeracClient,
    build_round1_payload,
    build_round2_payload,
    participant_task_url,
)
from app.config import settings
from app.db import session_scope
from app.metrics import (
    ExperimentResult,
    evaluate,
    split_summaries,
    summarize_labels,
)
from app.models import (
    Assignment,
    Finding,
    Label,
    Preference,
    RawFinding,
    Report,
    Round,
    Scan,
    Veto,
)
from app.security import UnsafeTargetError, assert_safe_target, scan_text_for_pii
from app.sources.base import BugSource
from app.sources.evidence import is_public_base, is_public_url
from app.triage import (
    assign_round1,
    assign_round2,
    rank_v1,
    rank_v2,
    rationale_text,
    recalibrate,
    select_for_review,
)

logger = logging.getLogger(__name__)


def get_bug_source(name: str | None = None) -> BugSource:
    """Resolve the configured `BugSource`. The 11:00 gate is one env var.

    docs/DECISIONS.md 002. `PlaywrightSource` is the default because Replay's programmatic
    access was never verified (RESEARCH.md §7).
    """
    choice = (name or settings.bug_source).lower()

    if choice == "replay":
        from app.sources.replay import ReplayQASource

        return ReplayQASource()  # type: ignore[return-value]
    if choice == "seed":
        from app.sources.seed import SeedSource

        return SeedSource()  # type: ignore[return-value]

    from app.sources.playwright_source import PlaywrightSource

    return PlaywrightSource()  # type: ignore[return-value]


# ══════════════════════════════════════════════════════════════════════════════════════
# Stage 1 — scan and build v1
# ══════════════════════════════════════════════════════════════════════════════════════


def create_scan(url: str, *, order_id: str | None = None, source: str | None = None) -> str:
    """Register a scan. Validates the target before anything else touches it."""
    safe_url = assert_safe_target(url)
    with session_scope() as session:
        scan = Scan(url=safe_url, order_id=order_id, source=(source or settings.bug_source))
        session.add(scan)
        session.flush()
        logger.info("Created scan %s for %s", scan.id, safe_url)
        return scan.id


async def run_scan(scan_id: str) -> list[RawFinding]:
    """Execute the scan, persist findings, and write report v1.

    Idempotent: a scan that already has findings returns them rather than re-scanning, so a
    workflow retry does not double the finding set or hammer the target site.
    """
    with session_scope() as session:
        scan = session.get(Scan, scan_id)
        if scan is None:
            raise ValueError(f"Unknown scan {scan_id}")
        url = scan.url
        source_name = scan.source
        existing = session.scalars(select(Finding).where(Finding.scan_id == scan_id)).all()
        if existing:
            logger.info("Scan %s already has %d findings; not re-scanning.", scan_id, len(existing))
            return [f.to_raw() for f in existing]
        scan.status = "scanning"

    source = get_bug_source(source_name)

    try:
        if not await source.available():
            raise RuntimeError(
                f"BugSource {source.name!r} is not available. "
                "Set BUG_SOURCE=playwright, or BUG_SOURCE=seed to use the recorded "
                "fallback set (docs/SPECS.md §8, the 12:30 gate)."
            )
        findings = await source.scan(url, scan_id)
    except UnsafeTargetError:
        raise
    except Exception as exc:
        with session_scope() as session:
            scan = session.get(Scan, scan_id)
            if scan is not None:
                scan.status = "failed"
                scan.error = str(exc)[:1000]
        logger.exception("Scan %s failed", scan_id)
        raise

    with session_scope() as session:
        for finding in findings:
            session.add(
                Finding(
                    id=finding.id,
                    scan_id=scan_id,
                    journey=finding.journey,
                    step_intent=finding.step_intent,
                    expected=finding.expected,
                    observed=finding.observed,
                    screenshot_before_url=finding.screenshot_before_url,
                    screenshot_after_url=finding.screenshot_after_url,
                    console_errors=finding.console_errors,
                    failed_requests=finding.failed_requests,
                    source=finding.source,
                    agent_severity=finding.agent_severity,
                    agent_confidence=finding.agent_confidence,
                    category=finding.category,
                )
            )
        scan = session.get(Scan, scan_id)
        if scan is not None:
            scan.status = "scanned" if findings else "clean"

    if findings:
        write_report(scan_id, version=1, ranked=rank_v1(findings))

    logger.info("Scan %s produced %d findings", scan_id, len(findings))
    return findings


def load_findings(scan_id: str, session: Session | None = None) -> list[RawFinding]:
    if session is not None:
        rows = session.scalars(select(Finding).where(Finding.scan_id == scan_id)).all()
        return [row.to_raw() for row in rows]
    with session_scope() as scoped:
        rows = scoped.scalars(select(Finding).where(Finding.scan_id == scan_id)).all()
        return [row.to_raw() for row in rows]


def write_report(
    scan_id: str, *, version: int, ranked: Sequence[str], rationale: str | None = None
) -> None:
    """Upsert a report version."""
    with session_scope() as session:
        existing = session.scalars(
            select(Report).where(Report.scan_id == scan_id, Report.version == version)
        ).first()
        if existing is not None:
            existing.ranked_finding_ids = list(ranked)
            existing.rationale = rationale
        else:
            session.add(
                Report(
                    scan_id=scan_id,
                    version=version,
                    ranked_finding_ids=list(ranked),
                    rationale=rationale,
                )
            )


def get_report(scan_id: str, version: int) -> list[str]:
    with session_scope() as session:
        row = session.scalars(
            select(Report).where(Report.scan_id == scan_id, Report.version == version)
        ).first()
        return list(row.ranked_finding_ids or []) if row else []


# ══════════════════════════════════════════════════════════════════════════════════════
# Stage 2 — Round 1
# ══════════════════════════════════════════════════════════════════════════════════════

# A ceiling on how many people one round can hire. Both round-launching functions take a
# participant count that can originate in an LLM tool call, and that number multiplies real
# money. The cap is well above the planned panels (12 and 35) so it never binds in normal use.
MAX_PARTICIPANTS_PER_ROUND = 60


def _bounded_participants(requested: int | None, default: int, *, round_no: int) -> int:
    """How many people to hire, bounded.

    `requested or default` was wrong twice over: an unbounded integer is an unbounded charge,
    and because `or` treats 0 as absent, a request for *zero* participants silently became the
    full default panel — hiring twelve people in response to an input that asked for none.

    A non-positive request is refused rather than coerced. It only arises from a caller mistake,
    and quietly hiring somebody is the exact failure this function exists to prevent.
    """
    if requested is None:
        return default
    count = int(requested)
    if count <= 0:
        raise ValueError(
            f"Round {round_no} was asked for {count} participants. Refusing to guess: pass a "
            "positive count, or do not launch the round."
        )
    if count > MAX_PARTICIPANTS_PER_ROUND:
        logger.warning(
            "Clamped round-%d participants from %d to %d (limit).",
            round_no,
            count,
            MAX_PARTICIPANTS_PER_ROUND,
        )
        return MAX_PARTICIPANTS_PER_ROUND
    return count


def _launched_round(scan_id: str, round_no: int) -> dict[str, Any] | None:
    """The already-launched round for `(scan_id, round_no)`, if there is one.

    Used to make launching idempotent. Keyed on a *launched* opportunity id rather than the
    existence of a Round row, so a run that died between inserting the row and creating the
    opportunity can still be retried.
    """
    with session_scope() as session:
        row = session.scalars(
            select(Round).where(
                Round.scan_id == scan_id,
                Round.round_no == round_no,
                Round.opportunity_id.is_not(None),
            )
        ).first()
        if row is None:
            return None
        return {
            "scan_id": scan_id,
            "round_id": row.id,
            "opportunity_id": row.opportunity_id,
            "num_participants": row.num_participants,
            "already_launched": True,
        }


def prepare_round1(scan_id: str, *, num_participants: int | None = None) -> dict[str, Any]:
    """Select findings, build assignments, and persist them. No network calls.

    Split from `launch_round1` deliberately: the assignment is reviewable and the task pages
    are servable before a single credit is spent, which is what lets us test `/t/r1` on a
    phone before launching.
    """
    findings = load_findings(scan_id)
    if not findings:
        raise ValueError(f"Scan {scan_id} has no findings to verify.")

    # A finding with no screenshots cannot be adjudicated. The task page promises "two
    # screenshots" and would render none, so the only honest answer left is "Can't tell from
    # this evidence" — which CONFIRMING_ANSWERS excludes, so every category falls below the 30%
    # threshold and v2 gets recalibrated on an artifact of missing evidence. `is_public_base()`
    # cannot catch this: it inspects the base URL string, not whether any file exists.
    with_evidence = [f for f in findings if f.screenshot_before_url or f.screenshot_after_url]
    if not with_evidence:
        raise RuntimeError(
            f"Scan {scan_id} has {len(findings)} findings but none carry a screenshot. "
            "Refusing to pay people to judge evidence that does not exist. Check the bug "
            "source: ReplayQASource does not currently produce screenshots."
        )
    if len(with_evidence) < len(findings):
        logger.warning(
            "Excluding %d of %d findings from round 1: no screenshot evidence.",
            len(findings) - len(with_evidence),
            len(findings),
        )
    findings = with_evidence

    participants = _bounded_participants(num_participants, settings.r1_participants, round_no=1)
    per_participant = settings.r1_findings_per_participant
    raters = settings.r1_raters_per_finding

    # docs/DECISIONS.md 004 arithmetic: how many findings can this budget cover at
    # `raters` raters each?
    budget = max(1, (participants * per_participant) // max(raters, 1))
    selected = select_for_review(findings, budget=min(budget, len(findings)))

    assignments = assign_round1(
        selected,
        scan_id=scan_id,
        num_participants=participants,
        findings_per_participant=min(per_participant, len(selected)),
        raters_per_finding=raters,
    )

    with session_scope() as session:
        session.query(Assignment).filter(
            Assignment.scan_id == scan_id, Assignment.round_no == 1
        ).delete()
        for entry in assignments:
            session.add(
                Assignment(
                    scan_id=scan_id,
                    round_no=1,
                    slot=int(entry["slot"]),  # type: ignore[arg-type]
                    finding_ids=entry["finding_ids"],
                )
            )

    return {
        "scan_id": scan_id,
        "selected_finding_ids": selected,
        "n_selected": len(selected),
        "num_participants": participants,
        "findings_per_participant": min(per_participant, len(selected)),
        "total_judgments": participants * min(per_participant, len(selected)),
        "raters_per_finding": raters,
    }


async def launch_round1(scan_id: str, *, num_participants: int | None = None) -> dict[str, Any]:
    """Create and launch the Terac round-1 opportunity.

    Refuses to launch when evidence URLs are not publicly reachable. Twelve participants
    looking at broken images is a wasted round and unrecoverable at 13:30, so this is a hard
    stop rather than a warning.
    """
    # Idempotency. Launching is the moment credit is spent, and every caller above this can
    # retry: the Render task carries `max_retries=2`, the Recruiter agent can be re-prompted,
    # and the operator endpoint is a POST someone can double-click. Without this each retry
    # created a second opportunity and hired a second panel for the same scan.
    existing = _launched_round(scan_id, 1)
    if existing is not None:
        logger.warning(
            "Round 1 for scan %s is already launched as opportunity %s. Returning it instead "
            "of hiring a second panel.",
            scan_id,
            existing["opportunity_id"],
        )
        return existing

    plan = prepare_round1(scan_id, num_participants=num_participants)

    if not is_public_base():
        raise RuntimeError(
            "Evidence URLs are not public: "
            f"{settings.evidence_public_base!r}. Terac participants load screenshots from "
            "their own phones. Set PUBLIC_BASE_URL/EVIDENCE_BASE_URL to a public https host "
            "before launching (docs/SPECS.md §3.3)."
        )

    # And check the URLs we are actually about to ship. Evidence URLs are absolute and written
    # at capture time, so a scan that ran before PUBLIC_BASE_URL was pointed at the public host
    # has `localhost` frozen into these rows — which `is_public_base()` cannot see, because it
    # only inspects the current setting. This is precisely the 13:20 failure that check exists
    # to prevent, so it has to be checked against the stored data too.
    unreachable = [
        f.id
        for f in load_findings(scan_id)
        if f.id in set(plan["selected_finding_ids"])
        for url in (f.screenshot_before_url, f.screenshot_after_url)
        if url and not is_public_url(url)
    ]
    if unreachable:
        raise RuntimeError(
            f"{len(set(unreachable))} of the findings selected for round 1 have evidence URLs "
            "that a participant's phone cannot load (for example "
            f"{sorted(set(unreachable))[:3]}). These were captured while PUBLIC_BASE_URL pointed "
            "somewhere private. Re-run the scan with the public host set before launching."
        )

    async with TeracClient() as terac:
        project_id = await terac.ensure_project("Overwatch — verified QA")

        # Register the webhook before the first round goes live. `ensure_webhook` existed but
        # had no callers, so no subscription was ever created and `submission.approved` never
        # arrived — leaving `/api/scans/{id}/poll` as the only way labels reached us, which is a
        # manual step nobody remembers at 14:00.
        #
        # Best-effort by design: polling is the documented fallback (docs/DECISIONS.md 007), so a
        # webhook problem must not stop us hiring. `ensure_webhook` already treats the
        # already-registered 409 as success, which makes this idempotent across retries.
        try:
            subscription = await terac.ensure_webhook(f"{settings.public_base_url}/hooks/terac")
            logger.info("Terac webhook ready: %s", subscription.get("id") or "existing")
        except Exception as exc:
            logger.error(
                "Could not register the Terac webhook (%s). Continuing — labels will arrive via "
                "/api/scans/%s/poll instead.",
                exc,
                scan_id,
            )

        payload = build_round1_payload(
            project_id=project_id,
            task_url=participant_task_url(settings.public_base_url, f"/t/r1/{scan_id}"),
            num_participants=int(plan["num_participants"]),
            n_findings=int(plan["n_selected"]),
            findings_per_participant=int(plan["findings_per_participant"]),
        )

        with session_scope() as session:
            round_row = Round(
                scan_id=scan_id,
                round_no=1,
                project_id=project_id,
                num_participants=int(plan["num_participants"]),
                request_payload=payload,
            )
            session.add(round_row)
            session.flush()
            round_id = round_row.id

        opportunity = await terac.create_opportunity(payload)
        launched = await terac.launch_opportunity(opportunity.id)

    with session_scope() as session:
        round_row = session.get(Round, round_id)
        if round_row is not None:
            round_row.opportunity_id = opportunity.id
            round_row.status = "active"
            round_row.response_payload = launched if isinstance(launched, dict) else {}
            round_row.launched_at = datetime.now(UTC)
        scan = session.get(Scan, scan_id)
        if scan is not None:
            scan.status = "round1_live"

    logger.info("Round 1 launched: opportunity %s for scan %s", opportunity.id, scan_id)
    return {**plan, "opportunity_id": opportunity.id, "round_id": round_id}


# ══════════════════════════════════════════════════════════════════════════════════════
# Stage 3 — recalibrate into v2
# ══════════════════════════════════════════════════════════════════════════════════════


def build_v2(scan_id: str) -> dict[str, Any]:
    """Turn round-1 labels into report v2. Same findings, new ranking."""
    findings = load_findings(scan_id)
    if not findings:
        raise ValueError(f"Scan {scan_id} has no findings.")

    with session_scope() as session:
        labels = session.scalars(
            select(Label).where(Label.scan_id == scan_id, Label.round_no == 1)
        ).all()
        label_dicts = [
            {
                "finding_id": row.finding_id,
                "participant_id": row.participant_id,
                "is_real": row.is_real,
                "severity": row.severity,
            }
            for row in labels
        ]

    summaries = summarize_labels(label_dicts)
    if not summaries:
        logger.warning(
            "No round-1 labels for scan %s. v2 would be identical to v1; refusing to write "
            "a v2 that claims an improvement it cannot have.",
            scan_id,
        )
        return {"scan_id": scan_id, "n_labels": 0, "ranked": [], "written": False}

    recal = recalibrate(findings, summaries)
    ranked, recal = rank_v2(findings, summaries, recal)
    rationale = rationale_text(recal)

    write_report(scan_id, version=2, ranked=ranked, rationale=rationale)

    logger.info(
        "Report v2 written for %s from %d labels; dropped categories: %s",
        scan_id,
        len(label_dicts),
        recal.dropped_categories or "none",
    )
    return {
        "scan_id": scan_id,
        "n_labels": len(label_dicts),
        "ranked": ranked,
        "rationale": rationale,
        "dropped_categories": recal.dropped_categories,
        "written": True,
    }


# ══════════════════════════════════════════════════════════════════════════════════════
# Stage 4 — Round 2
# ══════════════════════════════════════════════════════════════════════════════════════


def prepare_round2(scan_id: str, *, num_participants: int | None = None) -> dict[str, Any]:
    """Build the round-2 assignment. No network calls, no credit spent.

    Split out for the same reason as `prepare_round1`: it makes `/t/r2/{scan_id}` servable
    before the opportunity exists, so the comparison page can be opened on a phone and checked
    while it is still free to fix.
    """
    v1 = get_report(scan_id, 1)
    v2 = get_report(scan_id, 2)
    if not v1 or not v2:
        raise ValueError(
            f"Scan {scan_id} needs both report versions before round 2 "
            f"(v1={len(v1)} ids, v2={len(v2)} ids). Run build_v2 first."
        )

    participants = _bounded_participants(num_participants, settings.r2_participants, round_no=2)

    with session_scope() as session:
        session.query(Assignment).filter(
            Assignment.scan_id == scan_id, Assignment.round_no == 2
        ).delete()
        entries = assign_round2(scan_id=scan_id, num_participants=participants)
        for entry in entries:
            session.add(
                Assignment(
                    scan_id=scan_id,
                    round_no=2,
                    slot=int(entry["slot"]),  # type: ignore[arg-type]
                    left_version=int(entry["left_version"]),  # type: ignore[arg-type]
                    finding_ids=[],
                )
            )

    return {
        "scan_id": scan_id,
        "num_participants": participants,
        # Randomised per slot so position does not carry the answer.
        "left_v1_slots": sum(1 for e in entries if int(e["left_version"]) == 1),  # type: ignore[arg-type]
        "left_v2_slots": sum(1 for e in entries if int(e["left_version"]) == 2),  # type: ignore[arg-type]
    }


async def launch_round2(scan_id: str, *, num_participants: int | None = None) -> dict[str, Any]:
    """Launch the fresh-panel comparison.

    Blocks rather than launching if round 1's opportunity id is unavailable: without the
    `reference--has_not_taken_study` filter, round-1 participants can judge their own work
    and "a fresh panel" is false (docs/DECISIONS.md 006, docs/AGENTS.md Recruiter).
    """
    # Round 2 hires the larger panel, so a duplicate launch here is the most expensive
    # mistake in the pipeline. See `launch_round1` for why retries are expected.
    existing = _launched_round(scan_id, 2)
    if existing is not None:
        logger.warning(
            "Round 2 for scan %s is already launched as opportunity %s. Returning it instead "
            "of hiring a second panel.",
            scan_id,
            existing["opportunity_id"],
        )
        return existing

    # `_launched_round`, not a plain `.first()`: a dead run that inserted a `Round` row and
    # died before setting `opportunity_id` (or a stale manual test row) leaves more than one
    # round-1 row for this scan, and an unfiltered query can return the empty one — which
    # silently blocks round 2 with "no opportunity id" even though round 1 is live.
    round1 = _launched_round(scan_id, 1)
    round1_opportunity_id = round1["opportunity_id"] if round1 else None

    if not round1_opportunity_id:
        raise RuntimeError(
            "BLOCKED: cannot exclude round 1 participants — round 1 has no opportunity id."
        )

    prepared = prepare_round2(scan_id, num_participants=num_participants)
    participants = int(prepared["num_participants"])

    async with TeracClient() as terac:
        project_id = await terac.ensure_project("Overwatch — verified QA")
        payload = build_round2_payload(
            project_id=project_id,
            task_url=participant_task_url(settings.public_base_url, f"/t/r2/{scan_id}"),
            num_participants=participants,
            round1_opportunity_id=round1_opportunity_id,
        )

        with session_scope() as session:
            round_row = Round(
                scan_id=scan_id,
                round_no=2,
                project_id=project_id,
                num_participants=participants,
                request_payload=payload,
            )
            session.add(round_row)
            session.flush()
            round_id = round_row.id

        opportunity = await terac.create_opportunity(payload)
        launched = await terac.launch_opportunity(opportunity.id)

    with session_scope() as session:
        round_row = session.get(Round, round_id)
        if round_row is not None:
            round_row.opportunity_id = opportunity.id
            round_row.status = "active"
            round_row.response_payload = launched if isinstance(launched, dict) else {}
            round_row.launched_at = datetime.now(UTC)
        scan = session.get(Scan, scan_id)
        if scan is not None:
            scan.status = "round2_live"

    logger.info("Round 2 launched: opportunity %s for scan %s", opportunity.id, scan_id)
    return {
        "scan_id": scan_id,
        "opportunity_id": opportunity.id,
        "round_id": round_id,
        "num_participants": participants,
        "excluded_opportunity_id": round1_opportunity_id,
    }


# ══════════════════════════════════════════════════════════════════════════════════════
# Results and governance
# ══════════════════════════════════════════════════════════════════════════════════════


def results(scan_id: str) -> ExperimentResult:
    """The numbers. Everything the dashboard renders comes from here.

    Builds a **second, held-out v2** alongside the real one: the labels are split by finding,
    v2 is rebuilt from the fit half only, and both rankings are then scored on the eval half
    that neither has seen. Without this the headline compares v2 against the very labels that
    built it, which reports a large win even on pure noise (see `app/metrics` docstring).

    The held-out ranking is never written to the database. It exists only to be measured — the
    report a customer receives is the full-label v2, because for the *customer* using every
    label is strictly better. The split is an evaluation device, not a product decision.
    """
    with session_scope() as session:
        finding_rows = session.scalars(select(Finding).where(Finding.scan_id == scan_id)).all()
        raw_findings = [row.to_raw() for row in finding_rows]
        findings = [{"id": f.id, "category": f.category} for f in finding_rows]
        label_dicts = [
            {
                "finding_id": label.finding_id,
                "participant_id": label.participant_id,
                "is_real": label.is_real,
                "severity": label.severity,
            }
            for label in session.scalars(
                select(Label).where(Label.scan_id == scan_id, Label.round_no == 1)
            ).all()
        ]
        preferences = [
            {"chose_version": p.chose_version}
            for p in session.scalars(select(Preference).where(Preference.scan_id == scan_id)).all()
        ]

    ranked_v2_holdout: list[str] | None = None
    if raw_findings and label_dicts:
        fit, held = split_summaries(summarize_labels(label_dicts))
        if fit and held:
            ranked_v2_holdout, _ = rank_v2(raw_findings, fit)

    return evaluate(
        findings=findings,
        labels=label_dicts,
        ranked_v1=get_report(scan_id, 1),
        ranked_v2=get_report(scan_id, 2),
        ranked_v2_holdout=ranked_v2_holdout,
        preferences=preferences,
    )


def pii_prescreen(scan_id: str) -> list[dict[str, Any]]:
    """Text-level PII pre-screen over evidence that will be shown to strangers.

    A cheap first pass for Critic over console errors and observed strings, which is where
    leaked emails and tokens actually appear. It is **not** the vision check on screenshot
    pixels that SPECS.md §9 describes — `app/agents/critic.py` states the same limitation
    where it consumes this.
    """
    hits: list[dict[str, Any]] = []
    with session_scope() as session:
        findings = session.scalars(select(Finding).where(Finding.scan_id == scan_id)).all()
        for finding in findings:
            blob = " ".join(
                [
                    finding.observed or "",
                    finding.expected or "",
                    " ".join(finding.console_errors or []),
                    " ".join(finding.failed_requests or []),
                ]
            )
            kinds = scan_text_for_pii(blob)
            if kinds:
                hits.append({"finding_id": finding.id, "kinds": kinds})
                finding.pii_blocked = True
    return hits


def record_veto(scan_id: str, reason: str, finding_id: str | None = None) -> str:
    """Record a Critic veto against a scan.

    Validates that the scan exists. Without that check a veto naming a scan id the model got
    slightly wrong was stored happily and gated nothing: `open_vetoes` for the real scan stayed
    empty, `release_report` released, and Critic had every reason to believe it had blocked. A
    veto that silently fails is worse than no veto, because it is the one governance property
    the whole crew is built to demonstrate.
    """
    with session_scope() as session:
        if session.get(Scan, scan_id) is None:
            raise ValueError(
                f"Cannot veto unknown scan {scan_id!r}. Refusing to record a block that would "
                "gate nothing."
            )
        if finding_id is not None and session.get(Finding, finding_id) is None:
            raise ValueError(f"Cannot veto unknown finding {finding_id!r} on scan {scan_id!r}.")
        veto = Veto(scan_id=scan_id, reason=reason, finding_id=finding_id)
        session.add(veto)
        session.flush()
        return veto.id


def clear_veto(veto_id: str) -> dict[str, Any]:
    """Lift a veto.

    Exposed only to Critic's toolset (`app/agents/critic.py`). docs/AGENTS.md makes a veto
    terminal until *Critic* lifts it, so Bursar never gets a handle on this function — the
    asymmetry is the governance property, and a shared tool list would erase it.
    """
    with session_scope() as session:
        veto = session.get(Veto, veto_id)
        if veto is None:
            raise ValueError(f"Unknown veto {veto_id}")
        veto.cleared = True
        return {"veto_id": veto_id, "cleared": True, "scan_id": veto.scan_id}


def open_vetoes(scan_id: str) -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = session.scalars(
            select(Veto).where(Veto.scan_id == scan_id, Veto.cleared.is_(False))
        ).all()
        return [{"id": r.id, "reason": r.reason, "finding_id": r.finding_id} for r in rows]


def release_report(scan_id: str) -> dict[str, Any]:
    """Bursar's release. Terminal on an open Critic veto.

    docs/AGENTS.md, Bursar: *"You never override Critic. A veto is terminal until Critic
    lifts it."* Enforced here rather than only in a prompt, so the governance property holds
    even if a model decides otherwise.
    """
    blocks = open_vetoes(scan_id)
    if blocks:
        return {
            "released": False,
            "reason": "BLOCKED by Critic",
            "vetoes": blocks,
        }
    with session_scope() as session:
        scan = session.get(Scan, scan_id)
        if scan is None:
            raise ValueError(f"Unknown scan {scan_id}")
        scan.released = True
        scan.status = "released"
    return {"released": True, "report_url": f"{settings.public_base_url}/report/{scan_id}"}
