"""The tools the Band crew acts through.

Two design rules here, both of which exist for the Band track's judging criteria rather than
for convenience:

**Least privilege.** Each agent gets only its own tools. Recruiter is the only agent holding a
function that talks to Terac, and Critic is the only agent that can lift a veto. docs/AGENTS.md
states both as rules of conduct; putting them in the tool graph makes them properties of the
system instead of instructions a model may ignore.

**Real side effects.** Every tool calls into `app.pipeline`, the same module the HTTP API uses.
Nothing here simulates work. That is what makes the delete test in docs/AGENTS.md answerable:
remove the room and these handoffs have no other channel.

Tools return compact JSON strings. An LLM reading a dict repr will invent fields; a JSON string
it must parse is harder to embellish.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from app import pipeline, triage
from app.config import settings
from app.security import UnsafeTargetError


def _json(payload: Any) -> str:
    return json.dumps(payload, default=str, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════════════
# Scout
# ══════════════════════════════════════════════════════════════════════════════════════


@tool
async def scan_url(url: str) -> str:
    """Scan a URL for bugs and persist the findings. Returns the scan id and finding counts.

    Takes several minutes. Call it once per URL and wait for it to return.
    """
    try:
        scan_id = pipeline.create_scan(url)
    except UnsafeTargetError as exc:
        return _json({"error": "unsafe_target", "detail": str(exc), "url": url})

    try:
        findings = await pipeline.run_scan(scan_id)
    except Exception as exc:
        # Surfaced rather than raised: docs/AGENTS.md tells Scout to report a structural
        # blocker to Triage, which it cannot do if the tool call blows up the turn.
        return _json({"scan_id": scan_id, "error": type(exc).__name__, "detail": str(exc)})

    low_confidence = sum(1 for f in findings if f.agent_confidence < 0.5)
    return _json(
        {
            "scan_id": scan_id,
            "source": settings.bug_source,
            "total_findings": len(findings),
            "low_confidence": low_confidence,
            "categories": sorted({f.category for f in findings}),
            "report_url": f"{settings.public_base_url}/report/{scan_id}",
        }
    )


@tool
def list_findings(scan_id: str) -> str:
    """List the findings for a scan: id, journey, category, severity, confidence."""
    findings = pipeline.load_findings(scan_id)
    return _json(
        [
            {
                "id": f.id,
                "journey": f.journey,
                "category": f.category,
                "severity": f.agent_severity,
                "confidence": round(f.agent_confidence, 3),
                "observed": f.observed[:200],
            }
            for f in findings
        ]
    )


# ══════════════════════════════════════════════════════════════════════════════════════
# Triage Officer
# ══════════════════════════════════════════════════════════════════════════════════════


@tool
def rank_v1(scan_id: str) -> str:
    """Produce report v1 — the baseline ranking, before any human input. Saves it."""
    findings = pipeline.load_findings(scan_id)
    if not findings:
        return _json({"error": "no_findings", "scan_id": scan_id})
    ranked = triage.rank_v1(findings)
    pipeline.write_report(scan_id, version=1, ranked=ranked)
    return _json({"scan_id": scan_id, "version": 1, "ranked_finding_ids": ranked})


@tool
def select_for_verification(scan_id: str, budget: int = 0) -> str:
    """Pick the findings worth paying humans to check, and build the round-1 assignment.

    Chooses the findings the machine is least sure about — verifying what we already believe
    is the wasted spend. Makes no network calls and spends no credit.
    """
    try:
        prepared = pipeline.prepare_round1(scan_id, num_participants=budget if budget > 0 else None)
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)})
    return _json(prepared)


@tool
def recalibrate_to_v2(scan_id: str) -> str:
    """Re-rank the SAME findings using the human labels from round 1. Produces report v2.

    Returns what changed and why. Never adds, removes, or edits a finding.
    """
    try:
        return _json(pipeline.build_v2(scan_id))
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)})


# ══════════════════════════════════════════════════════════════════════════════════════
# Recruiter — the only agent holding the Terac key
# ══════════════════════════════════════════════════════════════════════════════════════


@tool
async def launch_verification_round(scan_id: str, num_participants: int = 0) -> str:
    """Hire humans on Terac to verify round-1 findings. Returns the opportunity id.

    Spends real credit. Refuses to launch if the evidence screenshots are not publicly
    reachable, because participants would be judging broken images.
    """
    try:
        return _json(
            await pipeline.launch_round1(
                scan_id, num_participants=num_participants if num_participants > 0 else None
            )
        )
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)})


@tool
async def launch_comparison_round(scan_id: str, num_participants: int = 0) -> str:
    """Hire a FRESH panel on Terac to compare report v1 against v2.

    Carries the has_not_taken_study filter excluding round 1's opportunity. Refuses to launch
    if that filter cannot be built — an unfiltered round 2 contaminates the whole result.
    """
    try:
        return _json(
            await pipeline.launch_round2(
                scan_id, num_participants=num_participants if num_participants > 0 else None
            )
        )
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)})


@tool
def round_status(scan_id: str, round_no: int = 1) -> str:
    """How many labels are in for a round, and the confirmation rate so far."""
    result = pipeline.results(scan_id)
    return _json(
        {
            "scan_id": scan_id,
            "round_no": round_no,
            "n_labels": result.n_labels,
            "n_raters": result.n_raters,
            "n_preferences": result.n_preferences,
            "confirmation_rate": result.confirmation_rate,
        }
    )


# ══════════════════════════════════════════════════════════════════════════════════════
# Bursar — money in, report out. No veto handle.
# ══════════════════════════════════════════════════════════════════════════════════════


@tool
def check_blocks(scan_id: str) -> str:
    """List Critic's open vetoes on a scan. Empty list means nothing is blocking release."""
    return _json({"scan_id": scan_id, "open_vetoes": pipeline.open_vetoes(scan_id)})


@tool
def release(scan_id: str) -> str:
    """Deliver the report to the customer.

    Returns released:false while any Critic veto is open. Bursar cannot override that, and
    there is no argument to this tool that changes it.
    """
    try:
        return _json(pipeline.release_report(scan_id))
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)})


# ══════════════════════════════════════════════════════════════════════════════════════
# Critic — the only agent that can block, and the only one that can unblock
# ══════════════════════════════════════════════════════════════════════════════════════


@tool
def inspect_for_release(scan_id: str) -> str:
    """Everything Critic needs for its three checks, in one call.

    Returns the top-10 findings with their evidence text, the text-level PII hits, and the
    human confirmation rate.

    The PII field is a TEXT scan over console output and observed strings. It does not read
    screenshot pixels, so it cannot see a name rendered in an image — say so rather than
    reporting a clean text scan as a clean PII check.
    """
    ranked = pipeline.get_report(scan_id, 2) or pipeline.get_report(scan_id, 1)
    findings = {f.id: f for f in pipeline.load_findings(scan_id)}
    result = pipeline.results(scan_id)

    top = []
    for finding_id in ranked[:10]:
        finding = findings.get(finding_id)
        if finding is None:
            continue
        top.append(
            {
                "id": finding.id,
                "journey": finding.journey,
                "severity": finding.severity,
                "observed": finding.observed,
                "console_errors": finding.console_errors[:3],
                "screenshots": [s for s in (finding.before, finding.after) if s],
            }
        )

    return _json(
        {
            "scan_id": scan_id,
            "top_findings": top,
            "pii_text_hits": pipeline.pii_prescreen(scan_id),
            "pii_scan_covers": "console text and observed strings only, not screenshot pixels",
            "confirmation_rate": result.confirmation_rate,
            "n_labels": result.n_labels,
            "confirmation_threshold": 0.40,
        }
    )


@tool
def block_release(scan_id: str, reason: str, finding_id: str = "") -> str:
    """Veto a report. Blocks release until you lift it. Bursar cannot override this."""
    veto_id = pipeline.record_veto(scan_id, reason, finding_id or None)
    return _json({"veto_id": veto_id, "scan_id": scan_id, "reason": reason, "blocking": True})


@tool
def lift_block(veto_id: str) -> str:
    """Lift one of your own vetoes once the problem is fixed."""
    try:
        return _json(pipeline.clear_veto(veto_id))
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)})


# ══════════════════════════════════════════════════════════════════════════════════════
# Per-agent toolsets
# ══════════════════════════════════════════════════════════════════════════════════════

TOOLSETS: dict[str, list[Any]] = {
    "scout": [scan_url],
    "triage": [list_findings, rank_v1, select_for_verification, recalibrate_to_v2],
    # Only Recruiter. docs/AGENTS.md: "You are the only agent that talks to Terac."
    "recruiter": [launch_verification_round, launch_comparison_round, round_status],
    # No veto handle, by design — and no `scan_url` either. docs/AGENTS.md §4 says Bursar
    # "@Scout with the URL to start the scan"; holding the tool itself gave it a direct path
    # that bypasses the room, which is exactly the dependency the delete test claims exists
    # ("remove Band and the pipeline halts after Scout"). Delegation must be the only route.
    "bursar": [check_blocks, release],
    "critic": [inspect_for_release, block_release, lift_block],
}


def toolset(agent_key: str) -> list[Any]:
    try:
        return TOOLSETS[agent_key]
    except KeyError:
        raise ValueError(
            f"Unknown agent {agent_key!r}. Expected one of: {', '.join(sorted(TOOLSETS))}."
        ) from None


__all__ = ["TOOLSETS", "toolset"]
