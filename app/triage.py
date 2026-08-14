"""Triage: the v1 baseline, the rater assignment, and the v2 recalibration.

**This module is the intervention.** SPECS.md §2 in code:

| stage | function |
|---|---|
| Baseline v1 | `rank_v1` — the machine's own opinion, deliberately naive |
| Which findings to pay for | `select_for_review` |
| Who judges what | `assign_round1` |
| After v2 | `rank_v2` — same findings, new ranking |

The one invariant that makes the experiment valid: **v1 and v2 rank the identical finding
set.** Nothing here creates, edits, or deletes a finding. If a future change makes v2 rank a
different set, the comparison stops measuring triage quality and starts measuring which
scan got luckier, and the whole submission is void.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.metrics import LabelSummary, confirmation_rate_by_category
from app.models import SEVERITY_RANK, SEVERITY_WEIGHT, RawFinding

logger = logging.getLogger(__name__)

# SPECS.md §2: "drop finding categories with <30% confirmation".
CATEGORY_DROP_THRESHOLD = 0.30
# A category needs this many rater judgments before we trust its rate enough to demote it.
# Two raters disagreeing is not evidence that a whole category is noise.
MIN_RATERS_FOR_CATEGORY_JUDGMENT = 4


@dataclass(frozen=True)
class Recalibration:
    """What the human labels changed, in a form the report and dashboard can display."""

    dropped_categories: list[str]
    category_rates: dict[str, float]
    promoted: list[str]
    demoted: list[str]
    n_labels_used: int


def _seeded_rng(scan_id: str, salt: str = "") -> random.Random:
    """Deterministic per-scan randomness.

    Assignment and ordering must be reproducible: a page refresh must not reshuffle which
    findings a participant is judging, and we have to be able to reconstruct the
    assignment after the fact when explaining the method.
    """
    return random.Random(f"{scan_id}:{salt}")


# ══════════════════════════════════════════════════════════════════════════════════════
# v1 — the baseline
# ══════════════════════════════════════════════════════════════════════════════════════


def _v1_score(finding: RawFinding) -> tuple[int, float]:
    """Sort key for the baseline: severity band first, then raw confidence.

    Deliberately naive, and it is *supposed* to be beatable. SPECS.md §2 describes the
    baseline as "noisy: false positives, wrong severity order" — that is the point of a
    baseline. Making v1 clever would mean measuring nothing, because there would be no
    headroom for human labels to improve on.
    """
    return (SEVERITY_RANK.get(finding.agent_severity, 9), -finding.agent_confidence)


def rank_v1(findings: Sequence[RawFinding]) -> list[str]:
    """Report v1. The machine's unaided ranking."""
    return [f.id for f in sorted(findings, key=_v1_score)]


# ══════════════════════════════════════════════════════════════════════════════════════
# Selection and assignment
# ══════════════════════════════════════════════════════════════════════════════════════


def select_for_review(
    findings: Sequence[RawFinding],
    *,
    budget: int,
) -> list[str]:
    """Choose which findings are worth paying humans to judge.

    docs/AGENTS.md, Triage: *"Select the findings where your own confidence is weakest.
    High-confidence findings do not need verification; spending credit on them is the waste
    that costs us the 'efficient signal' criterion."*

    So the primary key is distance from certainty — `|confidence - 0.5|` ascending — and
    severity is only a tiebreak. A blocker we are 93% sure about teaches us nothing; a
    console error we are 35% sure about is exactly the coin-flip a human resolves.

    The one exception: findings ranked inside the top 10 of v1 are always included even if
    confident, because `precision@10` is measured over that prefix and an unlabeled finding
    there is an unknown that weakens the headline number.
    """
    if not findings:
        return []

    top10 = set(rank_v1(findings)[:10])

    def uncertainty(f: RawFinding) -> tuple[float, int]:
        return (abs(f.agent_confidence - 0.5), SEVERITY_RANK.get(f.agent_severity, 9))

    forced = [f for f in findings if f.id in top10]
    rest = [f for f in findings if f.id not in top10]

    ordered = sorted(forced, key=uncertainty) + sorted(rest, key=uncertainty)
    return [f.id for f in ordered[:budget]]


def assign_round1(
    finding_ids: Sequence[str],
    *,
    scan_id: str,
    num_participants: int,
    findings_per_participant: int,
    raters_per_finding: int = 3,
) -> list[dict[str, object]]:
    """Build the round-1 assignment: one entry per participant slot.

    RESEARCH.md §10.7 is the reason this is not a random draw. Decision 004's arithmetic
    (12 x 5 = 60 judgments over 20 findings at 3 raters) only holds if assignment is
    *balanced*. Handing each participant a random 5 of 20 gives 3 raters per finding *in
    expectation* while leaving some findings with 1 rater and others with 6 — and
    "majority of 3" is then undefined for part of the set.

    A round-robin over a deterministic shuffle guarantees each finding appears exactly
    `ceil(slots * per_participant / n)` times, so every finding gets the same number of
    raters give or take one. Order *within* each participant's set is then shuffled
    independently, which addresses the rater-fatigue risk Decision 004 itself flags.
    """
    ids = list(finding_ids)
    if not ids or num_participants <= 0 or findings_per_participant <= 0:
        return []

    rng = _seeded_rng(scan_id, "assign-r1")
    pool = ids[:]
    rng.shuffle(pool)

    total_judgments = num_participants * findings_per_participant
    if total_judgments < len(pool) * raters_per_finding:
        logger.warning(
            "Round 1 budget gives %d judgments for %d findings; at %d raters each that "
            "covers only %d findings. Reduce the finding set or raise participants.",
            total_judgments,
            len(pool),
            raters_per_finding,
            total_judgments // max(raters_per_finding, 1),
        )

    # Deal from a repeating cycle of the shuffled pool. Consecutive slots therefore never
    # receive the same finding twice, and coverage stays flat.
    assignments: list[dict[str, object]] = []
    cursor = 0
    for slot in range(num_participants):
        chosen: list[str] = []
        guard = 0
        while len(chosen) < findings_per_participant and guard < len(pool) * 3:
            candidate = pool[cursor % len(pool)]
            cursor += 1
            guard += 1
            if candidate not in chosen:  # never show one participant the same finding twice
                chosen.append(candidate)

        order = chosen[:]
        _seeded_rng(scan_id, f"order-{slot}").shuffle(order)
        assignments.append({"slot": slot, "finding_ids": order})

    return assignments


def assign_round2(*, scan_id: str, num_participants: int) -> list[dict[str, object]]:
    """Build the round-2 assignment, randomizing which report sits on the left.

    SPECS.md §5.3: *"Randomize v1/v2 side per participant and persist the assignment. A
    fixed order is order bias."* Persisting matters as much as randomizing — recomputing on
    each request would let a refresh flip the sides mid-task.
    """
    rng = _seeded_rng(scan_id, "assign-r2")
    return [
        {"slot": slot, "left_version": rng.choice([1, 2]), "finding_ids": []}
        for slot in range(max(num_participants, 0))
    ]


# ══════════════════════════════════════════════════════════════════════════════════════
# v2 — the recalibration
# ══════════════════════════════════════════════════════════════════════════════════════


def recalibrate(
    findings: Sequence[RawFinding],
    summaries: Mapping[str, LabelSummary],
) -> Recalibration:
    """Work out what the human labels tell us, before applying it to a ranking."""
    rates_raw = confirmation_rate_by_category(
        [{"id": f.id, "category": f.category} for f in findings], summaries
    )

    category_rates: dict[str, float] = {}
    dropped: list[str] = []
    for category, (confirming, total) in rates_raw.items():
        if total == 0:
            continue
        rate = confirming / total
        category_rates[category] = rate
        if total >= MIN_RATERS_FOR_CATEGORY_JUDGMENT and rate < CATEGORY_DROP_THRESHOLD:
            dropped.append(category)

    promoted = [fid for fid, s in summaries.items() if s.confirmed and (s.mean_severity or 0) >= 4]
    demoted = [fid for fid, s in summaries.items() if not s.confirmed]

    return Recalibration(
        dropped_categories=sorted(dropped),
        category_rates=category_rates,
        promoted=sorted(promoted),
        demoted=sorted(demoted),
        n_labels_used=sum(s.n_raters for s in summaries.values()),
    )


def rank_v2(
    findings: Sequence[RawFinding],
    summaries: Mapping[str, LabelSummary],
    recal: Recalibration | None = None,
) -> tuple[list[str], Recalibration]:
    """Report v2. **Same findings, new ranking.**

    Three signals, in the order SPECS.md §2 lists them:

    1. **Confirmed/rejected exemplars.** A finding humans confirmed rises; one they rejected
       falls hard. This is the strongest signal and it is direct evidence, not a prior.
    2. **Mean human severity re-weights the ranking.** Where humans gave a severity, it
       replaces the machine's severity band, because a 1-5 mean from three people is a
       better estimate of "would a user care" than a single classifier's label.
    3. **Categories below 30% confirmation are demoted.** This generalizes from labeled
       findings to unlabeled ones in the same category — the only mechanism by which round 1
       improves the ranking of findings *nobody judged*, which is what makes the
       intervention worth more than the 20 labels it bought.

    A demoted category is pushed down rather than deleted. Dropping findings outright would
    change the set being ranked and break the comparison; and a category can be noisy
    without every member being false.
    """
    recal = recal or recalibrate(findings, summaries)
    dropped = set(recal.dropped_categories)

    def score(f: RawFinding) -> tuple[float, float, int]:
        summary = summaries.get(f.id)

        # Base: the machine's severity as a weight, blended with its own confidence.
        base = SEVERITY_WEIGHT.get(f.agent_severity, 0.3) * (0.5 + 0.5 * f.agent_confidence)

        if summary is not None:
            if summary.confirmed:
                # Human-confirmed severity replaces the machine's band. 1-5 -> 0.2-1.0.
                human_weight = (summary.mean_severity or 3.0) / 5.0
                base = 0.25 * base + 0.75 * human_weight
                base += 0.15 * summary.confirmation_rate
            else:
                # Explicitly rejected by a majority. This is the false positive the
                # baseline ranked too highly, and the clearest thing round 1 buys us.
                base *= 0.15

        if f.category in dropped:
            base *= 0.35

        # Negated for ascending sort. Ties fall back to severity band for stability.
        return (-base, -f.agent_confidence, SEVERITY_RANK.get(f.agent_severity, 9))

    ranked = [f.id for f in sorted(findings, key=score)]
    return ranked, recal


def rationale_text(recal: Recalibration) -> str:
    """Plain-language description of the intervention, for the report and the dashboard.

    Written out rather than left implicit because a judge asking "what actually changed?"
    should get a specific answer, not "we recalibrated".
    """
    parts = [
        f"Recalibrated using {recal.n_labels_used} human judgments.",
    ]
    if recal.promoted:
        parts.append(
            f"{len(recal.promoted)} finding(s) promoted after humans confirmed them at "
            "severity 4 or above."
        )
    if recal.demoted:
        parts.append(
            f"{len(recal.demoted)} finding(s) demoted after a majority of raters judged "
            "them not real."
        )
    if recal.dropped_categories:
        parts.append(
            "Categories demoted for confirming below "
            f"{int(CATEGORY_DROP_THRESHOLD * 100)}%: " + ", ".join(recal.dropped_categories) + "."
        )
    else:
        parts.append("No category fell below the 30% confirmation threshold.")
    return " ".join(parts)
