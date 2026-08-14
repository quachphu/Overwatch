"""Tests for the measurement.

`app/metrics.py` is the submission — the 40% "project improvement" score is whatever this
module prints. A silent bug here does not crash anything, it just reports a lift that is not
real, which is the single worst failure available to us. So the tests below target the specific
ways a metric lies rather than line coverage.
"""

from __future__ import annotations

import random
import statistics

import pytest

from app.metrics import (
    average_precision,
    evaluate,
    holdout_k,
    mean_average_precision,
    precision_at_k,
    split_summaries,
    summarize_labels,
    wilson_interval,
)
from app.models import RawFinding
from app.triage import rank_v1, rank_v2, select_for_review


def label(finding_id: str, participant: str, is_real: str, severity: int = 3):
    return {
        "finding_id": finding_id,
        "participant_id": participant,
        "is_real": is_real,
        "severity": severity,
    }


class TestSummarizeLabels:
    def test_strict_majority_not_mere_plurality(self):
        """1 of 2 raters is not a majority.

        The reason this is tested rather than assumed: with `>=` instead of `>`, every
        even-rater finding with one yes flips to confirmed and precision@10 inflates across
        the board.
        """
        summaries = summarize_labels(
            [label("f1", "p1", "clear_yes"), label("f1", "p2", "clear_no")]
        )
        assert summaries["f1"].n_raters == 2
        assert summaries["f1"].n_confirming == 1
        assert summaries["f1"].confirmed is False

    def test_two_of_three_confirms(self):
        summaries = summarize_labels(
            [
                label("f1", "p1", "clear_yes"),
                label("f1", "p2", "probably"),
                label("f1", "p3", "clear_no"),
            ]
        )
        assert summaries["f1"].confirmed is True

    def test_probably_counts_as_confirming_unsure_does_not(self):
        summaries = summarize_labels([label("f1", "p1", "probably"), label("f2", "p1", "unsure")])
        assert summaries["f1"].confirmed is True
        assert summaries["f2"].confirmed is False

    def test_mean_severity_averages_only_present_values(self):
        summaries = summarize_labels(
            [label("f1", "p1", "clear_yes", 5), label("f1", "p2", "clear_yes", 3)]
        )
        assert summaries["f1"].mean_severity == 4.0

    def test_labels_without_a_finding_id_are_dropped(self):
        assert summarize_labels([{"participant_id": "p1", "is_real": "clear_yes"}]) == {}


class TestPrecisionAtK:
    def test_unlabeled_findings_are_unknown_not_false(self):
        """The denominator is labeled findings only.

        Round 1 buys labels for 20 of N findings. Counting an unlabeled finding in the top 10
        as a miss would score our sampling budget instead of the ranking, and would penalise
        v1 and v2 identically for something neither did.
        """
        summaries = summarize_labels(
            [label("f1", "p1", "clear_yes"), label("f2", "p1", "clear_no")]
        )
        # f3..f10 are unlabeled: 1 confirmed of 2 judged, not 1 of 10.
        ranked = ["f1", "f2"] + [f"f{i}" for i in range(3, 11)]
        assert precision_at_k(ranked, summaries, k=10) == 0.5

    def test_none_when_nothing_in_the_prefix_was_labeled(self):
        summaries = summarize_labels([label("f99", "p1", "clear_yes")])
        assert precision_at_k(["f1", "f2"], summaries, k=10) is None

    def test_none_on_empty_ranking(self):
        assert precision_at_k([], {}, k=10) is None


class TestMeanAveragePrecision:
    def test_rewards_ranking_the_confirmed_finding_first(self):
        """This is why MAP is reported next to precision@10.

        Both orderings below have identical precision@10. MAP separates them, and "put the real
        bug at position 1" is the thing the intervention actually claims to do.
        """
        summaries = summarize_labels(
            [label("good", "p1", "clear_yes"), label("bad", "p1", "clear_no")]
        )
        assert precision_at_k(["good", "bad"], summaries) == precision_at_k(
            ["bad", "good"], summaries
        )
        assert mean_average_precision(["good", "bad"], summaries) > mean_average_precision(
            ["bad", "good"], summaries
        )

    def test_zero_when_nothing_confirmed(self):
        summaries = summarize_labels([label("f1", "p1", "clear_no")])
        assert mean_average_precision(["f1"], summaries) == 0.0


class TestWilsonInterval:
    def test_stays_inside_zero_one_at_the_extreme(self):
        """The reason we use Wilson and not the normal approximation.

        At 20/20 the normal interval runs past 1.0, which is visibly wrong on a slide. Wilson
        tops out at 1.0 exactly in real arithmetic, so this only asserts it does not exceed it.
        """
        low, high = wilson_interval(20, 20)
        assert 0.0 <= low <= high <= 1.0
        assert high == pytest.approx(1.0)
        assert low > 0.8

    def test_brackets_the_point_estimate(self):
        low, high = wilson_interval(7, 10)
        assert low < 0.7 < high

    def test_zero_n_is_not_a_crash(self):
        assert wilson_interval(0, 0) == (0.0, 0.0)

    def test_narrows_as_n_grows(self):
        small = wilson_interval(7, 10)
        large = wilson_interval(70, 100)
        assert (large[1] - large[0]) < (small[1] - small[0])


class TestSignificanceGuard:
    """SPECS.md §7: "A 70/30 split at n=35 is real; 55/45 is not."

    `preference_is_significant` is what the dashboard consults, so these two cases are the
    guard that stops us reporting noise as a win.
    """

    @staticmethod
    def _result(v2_votes: int, v1_votes: int):
        preferences = [{"chose_version": 2}] * v2_votes + [{"chose_version": 1}] * v1_votes
        return evaluate(findings=[], labels=[], ranked_v1=[], ranked_v2=[], preferences=preferences)

    def test_seventy_thirty_at_n35_is_significant(self):
        result = self._result(25, 10)
        assert result.n_preferences == 35
        assert result.preference_share_v2 is not None
        assert round(result.preference_share_v2, 2) == 0.71
        assert result.preference_is_significant is True

    def test_fifty_five_forty_five_is_not(self):
        result = self._result(19, 16)
        assert result.n_preferences == 35
        assert result.preference_is_significant is False

    def test_no_votes_is_not_significant(self):
        result = self._result(0, 0)
        assert result.preference_share_v2 is None
        assert result.preference_is_significant is False


class TestEvaluate:
    def test_counts_distinct_raters_not_labels(self):
        result = evaluate(
            findings=[{"id": "f1"}, {"id": "f2"}],
            labels=[
                label("f1", "p1", "clear_yes"),
                label("f2", "p1", "clear_yes"),
                label("f1", "p2", "clear_yes"),
            ],
            ranked_v1=["f1", "f2"],
            ranked_v2=["f2", "f1"],
            preferences=[],
        )
        assert result.n_labels == 3
        assert result.n_raters == 2

    def test_precision_delta_is_none_until_both_versions_scored(self):
        result = evaluate(findings=[], labels=[], ranked_v1=[], ranked_v2=[], preferences=[])
        assert result.precision_delta is None

    def test_unparseable_vote_does_not_destroy_the_whole_result(self):
        """`chose_version` arrives from a participant's form POST.

        A bare `int()` on a junk value raised ValueError out of `evaluate`, which would take
        down the entire results page — including the votes we already paid for — because one
        row was malformed.
        """
        result = evaluate(
            findings=[],
            labels=[],
            ranked_v1=[],
            ranked_v2=[],
            preferences=[
                {"chose_version": 2},
                {"chose_version": "not-a-number"},
                {"chose_version": None},
                {"chose_version": 1},
            ],
        )
        assert (result.preference_v2, result.preference_v1) == (1, 1)


class TestCondensedComparability:
    """Both rankings must be scored over the same judged pool.

    Filtering to labeled findings *after* truncating to k gave v1 and v2 different
    denominators, because v2 deliberately hoists labeled-confirmed findings into its top 10.
    """

    def test_denominator_is_identical_for_both_rankings(self):
        summaries = summarize_labels([label("a", "p1", "clear_yes"), label("b", "p1", "clear_no")])
        # v1 buries the judged findings behind unlabeled ones; v2 puts them first.
        v1 = ["u1", "u2", "u3", "a", "b"]
        v2 = ["a", "b", "u1", "u2", "u3"]
        # One of two judged findings is confirmed either way. Any difference here would be an
        # artifact of where the unlabeled findings sit, not of ranking quality.
        assert precision_at_k(v1, summaries, 2) == precision_at_k(v2, summaries, 2) == 0.5

    def test_unlabeled_findings_never_count_against_a_ranking(self):
        summaries = summarize_labels([label("a", "p1", "clear_yes")])
        assert precision_at_k(["a"] + [f"u{i}" for i in range(9)], summaries, 10) == 1.0


class TestAveragePrecisionNormalisation:
    def test_missing_relevant_findings_are_penalised(self):
        """Normalising by hits-found instead of relevant-total erases recall.

        Both rankings below put a confirmed finding first. The second also has four more
        confirmed findings that it ranks last. Dividing by hits scored both 1.0, so
        "found one real bug" tied with "ranked every real bug perfectly".
        """
        one_relevant = summarize_labels([label("a", "p1", "clear_yes")])
        many_relevant = summarize_labels(
            [label("a", "p1", "clear_yes")]
            + [label(f"r{i}", "p1", "clear_yes") for i in range(4)]
            + [label(f"n{i}", "p1", "clear_no") for i in range(4)]
        )
        perfect = average_precision(["a"], one_relevant, 10)
        partial = average_precision(
            ["a", "n0", "n1", "n2", "n3", "r0", "r1", "r2", "r3"], many_relevant, 10
        )
        assert perfect == pytest.approx(1.0)
        assert partial < perfect


class TestHoldoutIsNotCircular:
    """The property the whole submission rests on.

    Report v2 is fitted to the round-1 labels. Scoring it on those same labels measures the
    fitting procedure, so it reports a large win even when the labels are pure noise. These
    tests pin the difference between the in-sample diagnostic and the held-out evidence.
    """

    SEVERITIES = ["blocker", "major", "minor", "cosmetic"]
    CATEGORIES = ["console_error", "layout", "accessibility", "network", "content"]
    # Under `signal`, these two categories are mostly false positives — a learnable pattern.
    NOISY = {"layout", "accessibility"}

    def _trial(self, seed: int, mode: str):
        rng = random.Random(seed)
        findings = [
            RawFinding(
                scan_id="s",
                journey="j",
                step_intent="i",
                expected="e",
                observed="o",
                source="seed",
                agent_severity=rng.choice(self.SEVERITIES),
                agent_confidence=rng.random(),
                category=rng.choice(self.CATEGORIES),
            )
            for _ in range(44)
        ]
        by_id = {f.id: f for f in findings}
        labels = []
        for fid in select_for_review(findings, budget=20):
            p_confirm = (
                0.5 if mode == "noise" else (0.15 if by_id[fid].category in self.NOISY else 0.85)
            )
            for r in range(3):
                labels.append(
                    label(
                        fid,
                        f"p{r}",
                        "clear_yes" if rng.random() < p_confirm else "no",
                        rng.randint(1, 5),
                    )
                )

        summaries = summarize_labels(labels)
        fit, held = split_summaries(summaries)
        ranked_v1 = rank_v1(findings)
        ranked_v2, _ = rank_v2(findings, summaries)
        ranked_v2_holdout, _ = rank_v2(findings, fit)
        return evaluate(
            findings=[{"id": f.id, "category": f.category} for f in findings],
            labels=labels,
            ranked_v1=ranked_v1,
            ranked_v2=ranked_v2,
            ranked_v2_holdout=ranked_v2_holdout,
            preferences=[],
        )

    def _deltas(self, mode: str, trials: int = 120):
        in_sample, held_out = [], []
        for seed in range(trials):
            result = self._trial(seed, mode)
            if result.precision_delta is not None:
                in_sample.append(result.precision_delta)
            if result.precision_delta_holdout is not None:
                held_out.append(result.precision_delta_holdout)
        return in_sample, held_out

    def test_in_sample_reports_a_large_win_on_pure_noise(self):
        """Documents the bug this design exists to avoid.

        If this ever drops toward zero, the recalibration stopped using the labels at all and
        something upstream is broken — so it is asserted rather than merely commented.
        """
        in_sample, _ = self._deltas("noise")
        assert statistics.mean(in_sample) > 0.3

    def test_holdout_reports_no_win_on_pure_noise(self):
        """The guard. Coin-flip labels must not produce an improvement."""
        _, held_out = self._deltas("noise")
        assert abs(statistics.mean(held_out)) < 0.05
        wins = sum(1 for d in held_out if d > 0)
        losses = sum(1 for d in held_out if d < 0)
        # Symmetric within noise: neither direction should dominate.
        assert abs(wins - losses) <= max(6, 0.2 * (wins + losses))

    def test_holdout_detects_real_signal(self):
        """A metric that cannot move is broken, not conservative.

        Held-out precision@10 over a 10-finding eval half was rank-invariant and read exactly
        0.000 on signal as well as noise. `holdout_k` is what fixes that.
        """
        _, held_out = self._deltas("signal")
        assert statistics.mean(held_out) > 0.05
        assert sum(1 for d in held_out if d > 0) > sum(1 for d in held_out if d < 0)

    def test_holdout_k_is_smaller_than_the_eval_half(self):
        assert holdout_k(10, 10) == 5
        assert holdout_k(20, 10) == 10
        assert holdout_k(1, 10) == 1
        assert holdout_k(0, 10) == 1

    def test_no_holdout_claimed_when_too_few_findings_are_labeled(self):
        """One labeled finding cannot be split into a fit half and an eval half."""
        assert split_summaries(summarize_labels([label("f1", "p1", "clear_yes")])) == ({}, {})

    def test_holdout_absent_rather_than_falling_back_to_in_sample(self):
        """A missing held-out ranking must leave the honest figure `None`.

        Silently substituting the in-sample number would put the circular figure under the
        honest label, which is worse than showing nothing.
        """
        result = evaluate(
            findings=[{"id": "f1"}],
            labels=[label("f1", "p1", "clear_yes")],
            ranked_v1=["f1"],
            ranked_v2=["f1"],
            preferences=[],
        )
        assert result.precision_at_10_v1 is not None
        assert result.precision_delta_holdout is None
        assert result.has_holdout is False
