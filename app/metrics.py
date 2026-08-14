"""The measurement. SPECS.md §7.

This module is the submission. Everything else exists to produce its inputs, so it has no
dependencies beyond the standard library and stays independently testable.

Three rules govern every function here:

* Report `n`. A rate without a denominator is not a result.
* Never claim a win the interval does not support. `SPECS.md` §7: "A 70/30 split at n=35
  is real; 55/45 is not."
* **Never score a ranking on the labels that built it.** See below.

## Why there are two precision numbers

Report v2 is *fitted* to the round-1 labels: `triage.rank_v2` promotes findings humans
confirmed and multiplies rejected ones by 0.15. Scoring that ranking against those same labels
measures the fitting procedure, not the ranking — it is training on the test set.

It is not a small effect. Feed the pipeline pure coin-flip labels carrying zero information and
in-sample `precision@10` still goes 0.50 -> 1.00 in 400 of 400 simulated trials, because v2
sorts the known-confirmed findings to the top by construction. A metric that reports a large
win on noise cannot evidence a win on signal.

So every precision figure exists twice:

* `*_in_sample` — fitted and scored on all labels. A **diagnostic** that the recalibration is
  wired up and doing something. Never evidence, and the dashboard says so.
* `*_holdout` — the labels are split in half by finding; v2 is rebuilt from the *fit* half
  only, then both rankings are scored on the *eval* half, which neither has seen. This one can
  legitimately fail, which is exactly what makes it worth reporting.

The generalization being tested is real rather than tautological: `rank_v2`'s category demotion
learns "this category confirms below 30%" from the fit half and applies it to findings in the
eval half that no human judged. If human labels carry category-level signal, the held-out number
moves. If they do not, it should not.

Round 2's fresh-panel preference remains the primary result. This makes the ranking metric
defensible instead of circular.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from app.models import CONFIRMING_ANSWERS


@dataclass(frozen=True)
class LabelSummary:
    """Aggregated human judgment for one finding."""

    finding_id: str
    n_raters: int
    n_confirming: int
    mean_severity: float | None
    confirmed: bool

    @property
    def confirmation_rate(self) -> float:
        return self.n_confirming / self.n_raters if self.n_raters else 0.0


@dataclass
class ExperimentResult:
    """Everything the dashboard and the pitch need, computed once."""

    n_findings: int = 0
    n_labels: int = 0
    n_raters: int = 0
    n_findings_labeled: int = 0

    # In-sample: fitted and scored on the same labels. A wiring diagnostic, not evidence.
    precision_at_10_v1: float | None = None
    precision_at_10_v2: float | None = None
    map_v1: float | None = None
    map_v2: float | None = None

    # Held out: v2 rebuilt from the fit half, both rankings scored on the eval half. This is
    # the honest ranking number and the only one that can legitimately come out negative.
    precision_at_10_v1_holdout: float | None = None
    precision_at_10_v2_holdout: float | None = None
    map_v1_holdout: float | None = None
    map_v2_holdout: float | None = None
    n_holdout_fit: int = 0
    n_holdout_eval: int = 0
    # The `k` actually used for the held-out precision, which is smaller than the headline k —
    # see `holdout_k`. Reported so the dashboard can label the figure with its real k instead
    # of claiming "precision@10" for a precision@5.
    holdout_k: int = 0

    preference_v2: int = 0
    preference_v1: int = 0
    preference_share_v2: float | None = None
    preference_ci: tuple[float, float] | None = None

    confirmation_rate: float | None = None
    confirmation_ci: tuple[float, float] | None = None

    dropped_categories: list[str] = field(default_factory=list)
    judgments_per_finding: float | None = None

    @property
    def precision_delta(self) -> float | None:
        """In-sample delta. Near-guaranteed positive; see the module docstring."""
        if self.precision_at_10_v1 is None or self.precision_at_10_v2 is None:
            return None
        return self.precision_at_10_v2 - self.precision_at_10_v1

    @property
    def precision_delta_holdout(self) -> float | None:
        """The defensible ranking delta. This is the one to put on a slide."""
        if self.precision_at_10_v1_holdout is None or self.precision_at_10_v2_holdout is None:
            return None
        return self.precision_at_10_v2_holdout - self.precision_at_10_v1_holdout

    @property
    def map_delta_holdout(self) -> float | None:
        if self.map_v1_holdout is None or self.map_v2_holdout is None:
            return None
        return self.map_v2_holdout - self.map_v1_holdout

    @property
    def has_holdout(self) -> bool:
        return self.precision_delta_holdout is not None and self.n_holdout_eval > 0

    @property
    def n_preferences(self) -> int:
        return self.preference_v1 + self.preference_v2

    @property
    def preference_is_significant(self) -> bool:
        """True only when the Wilson interval excludes 50%.

        This is the guard that stops us reporting noise as a win. It is consulted by the
        dashboard template, not just by a human reading the number.
        """
        if self.preference_ci is None:
            return False
        low, high = self.preference_ci
        return low > 0.5 or high < 0.5


def summarize_labels(
    labels: Iterable[Mapping[str, object]],
    *,
    min_raters: int = 1,
) -> dict[str, LabelSummary]:
    """Collapse raw labels into one verdict per finding.

    "Confirmed" is a **strict majority** of that finding's raters answering `clear_yes` or
    `probably` (SPECS.md §7). Strict majority matters at even rater counts: 1 of 2 is not a
    majority, and treating it as one would inflate every precision number we report.
    """
    by_finding: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for label in labels:
        fid = str(label.get("finding_id") or "")
        if fid:
            by_finding[fid].append(label)

    out: dict[str, LabelSummary] = {}
    for fid, group in by_finding.items():
        n = len(group)
        if n < min_raters:
            continue
        confirming = sum(1 for g in group if str(g.get("is_real")) in CONFIRMING_ANSWERS)
        severities = [
            float(g["severity"]) for g in group if isinstance(g.get("severity"), int | float)
        ]
        out[fid] = LabelSummary(
            finding_id=fid,
            n_raters=n,
            n_confirming=confirming,
            mean_severity=(sum(severities) / len(severities)) if severities else None,
            confirmed=confirming * 2 > n,
        )
    return out


def _condense(ranked_ids: Sequence[str], summaries: Mapping[str, LabelSummary]) -> list[str]:
    """Drop unlabeled findings from a ranking, preserving order.

    Round 1 labels 20 of N findings, so an unlabeled finding is *unknown*, not *false*, and
    must not count against a ranking. The subtlety is **where** it gets dropped: filtering
    after truncating to `k` leaves the two rankings with different denominators, because v2
    deliberately hoists labeled-and-confirmed findings into its top 10 while v1 has whatever
    mix the scan produced. Comparing 9/9 against 4/4 is not a comparison.

    Condensing first — the standard treatment for incomplete relevance judgments — gives both
    rankings the same judged pool and therefore the same denominator, `min(k, n_judged)`.
    """
    return [fid for fid in ranked_ids if fid in summaries]


def precision_at_k(
    ranked_ids: Sequence[str],
    summaries: Mapping[str, LabelSummary],
    k: int = 10,
) -> float | None:
    """Fraction of the top `k` *judged* findings that humans confirmed.

    Returns `None` when nothing in the ranking was labeled, which is different from 0.0 and
    has to stay different: 0.0 means "humans rejected these", `None` means "we have no idea".
    """
    judged = _condense(ranked_ids, summaries)[:k]
    if not judged:
        return None
    return sum(1 for fid in judged if summaries[fid].confirmed) / len(judged)


def average_precision(
    ranked_ids: Sequence[str],
    summaries: Mapping[str, LabelSummary],
    k: int = 10,
) -> float | None:
    """Average precision at `k` over the judged ranking.

    Reported alongside `precision@k` because at 20 labeled findings `precision@10` moves in
    steps of 0.1 and is blind to *where* in the ranking a confirmed finding sits
    (RESEARCH.md §10.6). AP rewards putting real bugs at position 1 rather than 10.

    Normalised by `min(R, k)`, where `R` is the number of confirmed findings in the whole
    judged set. Dividing by the number of hits *found* instead — which this did originally —
    scores "one real bug, ranked first, nine missed" identically to "all ten ranked
    perfectly", both 1.0. That erases recall and defeats the reason the metric is here.
    """
    judged = _condense(ranked_ids, summaries)
    if not judged:
        return None

    relevant = sum(1 for fid in judged if summaries[fid].confirmed)
    if relevant == 0:
        return 0.0

    hits = 0
    total = 0.0
    for idx, fid in enumerate(judged[:k], start=1):
        if summaries[fid].confirmed:
            hits += 1
            total += hits / idx
    return total / min(relevant, k)


# Kept because "MAP" is the name used in docs/SPECS.md §7 and the dashboard. Strictly this is
# average precision for a single ranking; MAP is its mean over many queries, and we have one.
mean_average_precision = average_precision


def holdout_k(n_eval: int, k: int = 10) -> int:
    """Pick a `k` that can actually discriminate between rankings on the eval half.

    `precision@k` is **rank-invariant once `k >= n_judged`**: if the eval half holds 10
    findings and we ask for precision@10, the top 10 is all of them and every permutation
    scores identically. Held out precision@10 over a 10-finding eval half therefore reported a
    delta of exactly 0.000 in 400/400 trials on *signal* as well as on noise — a metric that
    cannot move is not a conservative metric, it is a broken one.

    Half the eval set keeps the question meaningful: "did the confirmed findings land in the
    better half of the ranking?"
    """
    if n_eval <= 1:
        return max(1, n_eval)
    return max(1, min(k, n_eval // 2))


def split_summaries(
    summaries: Mapping[str, LabelSummary],
    *,
    seed: str = "holdout",
    eval_fraction: float = 0.5,
) -> tuple[dict[str, LabelSummary], dict[str, LabelSummary]]:
    """Split labeled findings into `(fit, eval)` halves for held-out evaluation.

    Split by **finding**, not by rater: the unit v2 is fitted on is the finding-level verdict,
    so holding out individual raters would leak the verdict through the remaining ones.

    Deterministic given `seed` so the reported number is reproducible and cannot be reshuffled
    until it flatters us. Returns two empty dicts when there are fewer than two labeled
    findings, since a half of nothing supports no claim.
    """
    ids = sorted(summaries)
    if len(ids) < 2:
        return {}, {}

    rng = random.Random(seed)
    rng.shuffle(ids)
    n_eval = max(1, min(len(ids) - 1, round(len(ids) * eval_fraction)))
    eval_ids = set(ids[:n_eval])

    fit = {fid: s for fid, s in summaries.items() if fid not in eval_ids}
    held = {fid: s for fid, s in summaries.items() if fid in eval_ids}
    return fit, held


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Used instead of the normal approximation because at n=35 with a lopsided split the
    normal interval can extend past 1.0, which is visibly wrong on a slide.
    """
    if n <= 0:
        return (0.0, 0.0)

    # Clamp before dividing. `p > 1` makes `p * (1 - p)` negative and `math.sqrt` raise a
    # domain error, so a miscounted numerator would crash the dashboard rather than show a
    # slightly wrong bar.
    successes = max(0, min(successes, n))
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))

    low = (centre - margin) / denom
    high = (centre + margin) / denom
    return (max(0.0, low), min(1.0, high))


def confirmation_rate_by_category(
    findings: Iterable[Mapping[str, object]],
    summaries: Mapping[str, LabelSummary],
) -> dict[str, tuple[int, int]]:
    """Per-category `(confirming_raters, total_raters)`.

    Feeds the "drop categories below 30% confirmation" half of the intervention
    (SPECS.md §2). Aggregated over raters rather than over findings so that a category
    with one finding and three raters is not weighted the same as one with five findings.
    """
    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for finding in findings:
        fid = str(finding.get("id") or "")
        category = str(finding.get("category") or "uncategorized")
        summary = summaries.get(fid)
        if summary is None:
            continue
        tally[category][0] += summary.n_confirming
        tally[category][1] += summary.n_raters
    return {cat: (vals[0], vals[1]) for cat, vals in tally.items()}


def evaluate(
    *,
    findings: Sequence[Mapping[str, object]],
    labels: Sequence[Mapping[str, object]],
    ranked_v1: Sequence[str],
    ranked_v2: Sequence[str],
    preferences: Sequence[Mapping[str, object]],
    ranked_v2_holdout: Sequence[str] | None = None,
    k: int = 10,
) -> ExperimentResult:
    """Compute the full result set in one pass. The dashboard calls only this.

    `ranked_v2_holdout` must be a v2 ranking built from the *fit* half alone — see
    `pipeline.results`, which owns that step because ranking lives in `triage` and this module
    deliberately imports nothing from it. Omitting it leaves the held-out figures `None`, and
    the dashboard then shows no headline rather than showing the in-sample number in its place.
    """
    summaries = summarize_labels(labels)

    result = ExperimentResult(
        n_findings=len(findings),
        n_labels=len(labels),
        n_raters=len(
            {str(label.get("participant_id")) for label in labels if label.get("participant_id")}
        ),
        n_findings_labeled=len(summaries),
        precision_at_10_v1=precision_at_k(ranked_v1, summaries, k),
        precision_at_10_v2=precision_at_k(ranked_v2, summaries, k),
        map_v1=average_precision(ranked_v1, summaries, k),
        map_v2=average_precision(ranked_v2, summaries, k),
    )

    if ranked_v2_holdout is not None:
        fit, held = split_summaries(summaries)
        if held:
            hk = holdout_k(len(held), k)
            result.n_holdout_fit = len(fit)
            result.n_holdout_eval = len(held)
            result.holdout_k = hk
            result.precision_at_10_v1_holdout = precision_at_k(ranked_v1, held, hk)
            result.precision_at_10_v2_holdout = precision_at_k(ranked_v2_holdout, held, hk)
            # AP is rank-aware across the whole judged prefix, so it keeps the full k.
            result.map_v1_holdout = average_precision(ranked_v1, held, k)
            result.map_v2_holdout = average_precision(ranked_v2_holdout, held, k)

    if summaries:
        result.judgments_per_finding = result.n_labels / len(summaries)
        total_raters = sum(s.n_raters for s in summaries.values())
        total_confirming = sum(s.n_confirming for s in summaries.values())
        if total_raters:
            result.confirmation_rate = total_confirming / total_raters
            result.confirmation_ci = wilson_interval(total_confirming, total_raters)

    # `chose_version` originates in a participant's form POST. Coercing with a bare `int()`
    # turns one malformed value into a ValueError that takes down the whole results page —
    # including the round-2 votes we already paid for — so unparseable votes are skipped.
    def _vote(pref: Mapping[str, object]) -> int | None:
        try:
            return int(pref.get("chose_version"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    votes = [_vote(p) for p in preferences]
    v2_votes = sum(1 for v in votes if v == 2)
    v1_votes = sum(1 for v in votes if v == 1)
    result.preference_v2 = v2_votes
    result.preference_v1 = v1_votes
    total_votes = v1_votes + v2_votes
    if total_votes:
        result.preference_share_v2 = v2_votes / total_votes
        result.preference_ci = wilson_interval(v2_votes, total_votes)

    return result
