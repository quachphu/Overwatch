"""Tests for the ranking and the recalibration.

The claim these protect is the one the whole project rests on: **v1 and v2 rank the same
findings, and the only new information in v2 came from humans.** A bug that let v2 add, drop, or
edit a finding would invalidate the comparison without failing anything visibly.
"""

from __future__ import annotations

from app.metrics import summarize_labels
from app.models import RawFinding
from app.triage import (
    assign_round1,
    rank_v1,
    rank_v2,
    recalibrate,
    select_for_review,
)


def finding(
    fid: str,
    *,
    severity: str = "major",
    confidence: float = 0.5,
    category: str = "console_error",
) -> RawFinding:
    return RawFinding(
        id=fid,
        scan_id="scan_test",
        journey="checkout",
        step_intent="submit the form",
        expected="an order confirmation",
        observed="a 500 page",
        source="seed",
        agent_severity=severity,
        agent_confidence=confidence,
        category=category,
    )


def label(finding_id: str, participant: str, is_real: str, severity: int = 3):
    return {
        "finding_id": finding_id,
        "participant_id": participant,
        "is_real": is_real,
        "severity": severity,
    }


class TestRankV1:
    def test_orders_by_severity_then_confidence(self):
        findings = [
            finding("cosmetic", severity="cosmetic", confidence=0.99),
            finding("blocker", severity="blocker", confidence=0.20),
            finding("major_hi", severity="major", confidence=0.90),
            finding("major_lo", severity="major", confidence=0.10),
        ]
        assert rank_v1(findings) == ["blocker", "major_hi", "major_lo", "cosmetic"]

    def test_is_a_permutation_of_the_input(self):
        findings = [finding(f"f{i}", confidence=i / 10) for i in range(10)]
        assert sorted(rank_v1(findings)) == sorted(f.id for f in findings)


class TestSelectForReview:
    def test_prefers_coin_flips_over_certainties(self):
        """docs/AGENTS.md, Triage: verifying what we already believe is the wasted spend."""
        findings = [
            finding("certain", confidence=0.97),
            finding("coinflip", confidence=0.51),
            finding("confident", confidence=0.88),
        ]
        # Budget of 1, and all three are inside v1's top 10, so uncertainty decides.
        assert select_for_review(findings, budget=1) == ["coinflip"]

    def test_top_ten_is_always_included_even_when_confident(self):
        """precision@10 is measured over v1's top 10.

        An unlabeled finding there is an unknown that weakens the headline number, so the
        prefix is force-included ahead of uncertain findings further down.
        """
        findings = [finding(f"top{i}", severity="blocker", confidence=0.99) for i in range(10)]
        findings.append(finding("uncertain_tail", severity="cosmetic", confidence=0.5))

        selected = select_for_review(findings, budget=10)
        assert "uncertain_tail" not in selected
        assert len(selected) == 10

    def test_budget_is_a_ceiling(self):
        findings = [finding(f"f{i}") for i in range(30)]
        assert len(select_for_review(findings, budget=20)) == 20

    def test_empty_input(self):
        assert select_for_review([], budget=20) == []


class TestAssignRound1:
    """RESEARCH.md §10.7: a random draw gives 3 raters per finding *in expectation* while
    leaving some findings with 1 and others with 6, which makes "majority of 3" undefined for
    part of the set. Balance is the property, so balance is what is asserted."""

    def test_coverage_is_flat_within_one(self):
        ids = [f"f{i}" for i in range(20)]
        assignments = assign_round1(
            ids, scan_id="scan_x", num_participants=12, findings_per_participant=5
        )
        counts = {fid: 0 for fid in ids}
        for entry in assignments:
            for fid in entry["finding_ids"]:
                counts[fid] += 1
        assert max(counts.values()) - min(counts.values()) <= 1
        assert sum(counts.values()) == 60

    def test_no_participant_sees_a_finding_twice(self):
        assignments = assign_round1(
            [f"f{i}" for i in range(6)],
            scan_id="scan_x",
            num_participants=8,
            findings_per_participant=5,
        )
        for entry in assignments:
            assert len(set(entry["finding_ids"])) == len(entry["finding_ids"])

    def test_deterministic_for_a_scan(self):
        """A refresh must not reshuffle what a participant is judging."""
        args = dict(scan_id="scan_x", num_participants=12, findings_per_participant=5)
        ids = [f"f{i}" for i in range(20)]
        assert assign_round1(ids, **args) == assign_round1(ids, **args)

    def test_different_scans_differ(self):
        ids = [f"f{i}" for i in range(20)]
        a = assign_round1(ids, scan_id="scan_a", num_participants=12, findings_per_participant=5)
        b = assign_round1(ids, scan_id="scan_b", num_participants=12, findings_per_participant=5)
        assert a != b

    def test_degenerate_inputs_return_empty(self):
        assert assign_round1([], scan_id="s", num_participants=12, findings_per_participant=5) == []
        assert (
            assign_round1(["f1"], scan_id="s", num_participants=0, findings_per_participant=5) == []
        )


class TestRecalibrate:
    def test_drops_a_category_below_thirty_percent(self):
        findings = [finding(f"layout{i}", category="layout_shift") for i in range(4)]
        # 4 findings x 3 raters, all rejected: 0% confirmation for layout_shift.
        labels = [label(f.id, f"p{r}", "clear_no") for f in findings for r in range(3)]
        recal = recalibrate(findings, summarize_labels(labels))
        assert recal.dropped_categories == ["layout_shift"]

    def test_keeps_a_category_above_the_threshold(self):
        findings = [finding(f"err{i}", category="server_error") for i in range(4)]
        labels = [label(f.id, f"p{r}", "clear_yes") for f in findings for r in range(3)]
        recal = recalibrate(findings, summarize_labels(labels))
        assert recal.dropped_categories == []

    def test_promotes_only_confirmed_and_severe(self):
        findings = [finding("severe"), finding("mild")]
        labels = [
            label("severe", "p1", "clear_yes", 5),
            label("severe", "p2", "clear_yes", 5),
            label("mild", "p1", "clear_yes", 2),
            label("mild", "p2", "clear_yes", 2),
        ]
        recal = recalibrate(findings, summarize_labels(labels))
        assert recal.promoted == ["severe"]


class TestRankV2:
    def test_same_findings_new_order(self):
        """The invariant the experiment depends on. v2 re-ranks; it never edits the set."""
        findings = [finding(f"f{i}", confidence=i / 10) for i in range(10)]
        labels = [label("f0", "p1", "clear_yes", 5), label("f0", "p2", "clear_yes", 5)]
        ranked, _ = rank_v2(findings, summarize_labels(labels))
        assert sorted(ranked) == sorted(f.id for f in findings)
        assert len(ranked) == len(findings)

    def test_demotes_a_rejected_false_positive(self):
        """The clearest thing round 1 buys: a top-ranked finding humans say is not real."""
        findings = [
            finding("false_positive", severity="blocker", confidence=0.95),
            finding("real_bug", severity="major", confidence=0.60),
        ]
        assert rank_v1(findings)[0] == "false_positive"

        labels = [
            label("false_positive", "p1", "clear_no"),
            label("false_positive", "p2", "clear_no"),
            label("real_bug", "p1", "clear_yes", 5),
            label("real_bug", "p2", "clear_yes", 5),
        ]
        ranked, _ = rank_v2(findings, summarize_labels(labels))
        assert ranked[0] == "real_bug"
        assert ranked[-1] == "false_positive"

    def test_unlabeled_finding_moves_on_its_category(self):
        """The mechanism that makes 20 labels worth more than 20 findings.

        `unjudged` gets no labels of its own. It is demoted below an unrelated finding purely
        because its category confirmed badly — which is how round 1 improves the ranking of
        findings nobody looked at.
        """
        noisy = [finding(f"noisy{i}", category="layout_shift") for i in range(4)]
        unjudged = finding("unjudged", category="layout_shift", confidence=0.8)
        other = finding("other", category="server_error", confidence=0.8)

        labels = [label(f.id, f"p{r}", "clear_no") for f in noisy for r in range(3)]
        ranked, recal = rank_v2([*noisy, unjudged, other], summarize_labels(labels))

        assert "layout_shift" in recal.dropped_categories
        assert ranked.index("other") < ranked.index("unjudged")

    def test_no_labels_is_stable_not_a_crash(self):
        findings = [finding(f"f{i}", confidence=i / 10) for i in range(5)]
        ranked, recal = rank_v2(findings, {})
        assert sorted(ranked) == sorted(f.id for f in findings)
        assert recal.n_labels_used == 0
