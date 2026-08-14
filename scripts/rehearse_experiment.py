#!/usr/bin/env python3
"""Dry run of the whole experiment against a local server, with simulated raters.

**This produces no result. It is a plumbing check.**

The numbers it prints come from scripted judgments, not from people, and must never appear on a
slide or in the report. What it proves is narrower and still worth having before we spend
credit: that a scan, 60 form submissions, the recalibration, and the metrics are actually wired
to each other, and that `precision@10` can move at all. If v1 and v2 come out identical here,
they will come out identical on real labels too, and better to learn that now than at 16:00 with
twelve people already paid.

The simulated rater rejects the findings a careful human would plausibly reject — a broken
analytics script is not a user-facing bug, a decorative-image alt attribute is not a defect
worth a developer's afternoon — and confirms the ones that break a real user journey. That
pattern is a guess about human behaviour. Its only job is to be non-trivial enough that the
recalibration has something to do.

Usage:

    BUG_SOURCE=seed DATABASE_URL=sqlite:///./rehearsal.db \\
        python -m uvicorn app.main:app --port 8013 &
    python scripts/rehearse_experiment.py --base http://localhost:8013
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# Substrings identifying findings a thoughtful human would judge NOT real, or real but
# cosmetic. Matched against the observed text.
LIKELY_REJECTED = (
    "analytics is not defined",  # a broken analytics tag, not a user-facing failure
    "Text content did not match",  # hydration warning, invisible to users
    "Blocked aria-hidden",  # console noise
    'alt=""',  # decorative-image alt, debatable at best
    "no visible focus",  # real accessibility gap, but not what "bug" means to most raters
)

LIKELY_COSMETIC = (
    "overflowed by",
    "continued to scroll",
    "sku-4417.jpg",
    "/help/returns",
)


def request(method: str, url: str, fields: dict[str, str] | None = None) -> tuple[int, str]:
    data = urllib.parse.urlencode(fields).encode() if fields else None
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def judge(observed: str) -> tuple[str, str]:
    """One simulated rater's verdict on one finding.

    The verdicts must be exactly the values `t_r1.html` submits — `clear_yes`, `probably`, `no`,
    `cant_tell` — because this script stands in for a real participant's browser. It previously
    sent `clear_no`, which is not in `IsReal`: the negative verdicts were stored raw and only
    *happened* to read as non-confirming, and once the submit route began validating against the
    model they were dropped, silently costing the rehearsal every rejection it was designed to
    produce.
    """
    if any(marker in observed for marker in LIKELY_REJECTED):
        return "no", "1"
    if any(marker in observed for marker in LIKELY_COSMETIC):
        return "probably", "2"
    return "clear_yes", "5"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8013")
    parser.add_argument("--url", default="https://93.184.216.34/shop")
    parser.add_argument("--participants", type=int, default=12)
    args = parser.parse_args()
    base = args.base.rstrip("/")

    print("REHEARSAL — simulated raters. These numbers are not a result.\n")

    status, body = request("POST", f"{base}/api/scan?url={urllib.parse.quote(args.url)}")
    # The endpoint accepts a JSON or form body; a query string is neither, so post a form.
    if status >= 400:
        status, body = request("POST", f"{base}/api/scan", {"url": args.url})
    if status >= 400:
        print(f"scan failed: HTTP {status} {body[:300]}", file=sys.stderr)
        return 1

    scan_id = json.loads(body)["scan_id"]
    print(f"scan       {scan_id}")

    # The scan runs in a background task; poll until findings land.
    import time

    for _ in range(60):
        status, body = request("GET", f"{base}/api/scans/{scan_id}/results")
        if status == 200 and json.loads(body)["n_findings"]:
            break
        time.sleep(1)
    n_findings = json.loads(body)["n_findings"]
    print(f"findings   {n_findings}")
    if not n_findings:
        print("no findings — nothing to rehearse", file=sys.stderr)
        return 1

    sys.path.insert(0, ".")
    from app import pipeline

    prepared = pipeline.prepare_round1(scan_id)
    print(
        f"selected   {prepared['n_selected']} findings for "
        f"{prepared['num_participants']} participants "
        f"({prepared['total_judgments']} judgments, "
        f"{prepared['raters_per_finding']} raters each)"
    )

    observed = {f.id: f.observed for f in pipeline.load_findings(scan_id)}

    submitted = 0
    for i in range(1, args.participants + 1):
        pid = f"rehearsal_p{i}"
        status, html = request("GET", f"{base}/t/r1/{scan_id}?pid={pid}")
        ids = sorted(set(re.findall(r'name="is_real_(find_[0-9a-f]+)"', html)))
        if not ids:
            continue
        fields = {"participant_id": pid, "scan_id": scan_id}
        for fid in ids:
            verdict, severity = judge(observed.get(fid, ""))
            fields[f"is_real_{fid}"] = verdict
            fields[f"severity_{fid}"] = severity
        status, done_html = request("POST", f"{base}/t/r1/{scan_id}", fields)
        if status >= 400:
            print(f"{pid}: submit failed HTTP {status}", file=sys.stderr)
            return 1
        # A 200 is not enough. The route accepts a submission and skips any individual label it
        # cannot validate, so a rehearsal that posts a malformed verdict would otherwise look
        # healthy while quietly losing exactly the judgments it exists to produce.
        if "did not reach us intact" in done_html:
            print(
                f"{pid}: the server rejected one or more labels. The verdicts this script sends "
                "must match the values in t_r1.html.",
                file=sys.stderr,
            )
            return 1
        submitted += len(ids)
    print(f"judgments  {submitted} submitted")

    status, body = request("POST", f"{base}/api/scans/{scan_id}/v2")
    if status >= 400:
        print(f"v2 failed: HTTP {status} {body[:300]}", file=sys.stderr)
        return 1
    v2 = json.loads(body)
    print(f"v2         {v2['rationale']}")

    status, body = request("GET", f"{base}/api/scans/{scan_id}/results")
    result = json.loads(body)

    def fmt(value: object) -> str:
        return "—" if value is None else f"{float(value):.3f}"  # type: ignore[arg-type]

    print("\n── metrics " + "─" * 60)
    for key in ("n_labels", "n_raters", "confirmation_rate"):
        print(f"{key:<26} {result[key]}")

    hk = result.get("holdout_k") or 10
    h1, h2 = result.get("precision_at_10_v1_holdout"), result.get("precision_at_10_v2_holdout")
    print("\n  HELD OUT — the honest number (v2 rebuilt without these labels)")
    print(f"{'  precision@' + str(hk) + ' v1':<26} {fmt(h1)}")
    print(f"{'  precision@' + str(hk) + ' v2':<26} {fmt(h2)}")
    print(f"{'  AP v1':<26} {fmt(result.get('map_v1_holdout'))}")
    print(f"{'  AP v2':<26} {fmt(result.get('map_v2_holdout'))}")
    print(
        f"{'  fit / eval findings':<26} {result.get('n_holdout_fit')} / {result.get('n_holdout_eval')}"
    )

    print("\n  IN SAMPLE — circular, diagnostic only")
    print(f"{'  precision@10 v1':<26} {fmt(result['precision_at_10_v1'])}")
    print(f"{'  precision@10 v2':<26} {fmt(result['precision_at_10_v2'])}")

    print()
    if h1 is None or h2 is None:
        print(
            "No held-out figure: fewer than two findings carry labels, so the labels cannot "
            "be split. Nothing here supports an improvement claim."
        )
        return 1

    delta = h2 - h1
    if delta > 0:
        print(f"v2 beat v1 by {delta:+.3f} on held-out simulated labels. The wiring works.")
    elif delta == 0:
        print(
            f"v2 tied v1 at {h1:.3f} on held-out labels. Check the AP row, which is finer "
            "grained — and note a tie here is a legitimate outcome, not a bug."
        )
    else:
        print(
            f"v2 LOST to v1 by {delta:+.3f} on held-out labels. Investigate before spending credit."
        )

    in_delta = None
    if result["precision_at_10_v1"] is not None and result["precision_at_10_v2"] is not None:
        in_delta = result["precision_at_10_v2"] - result["precision_at_10_v1"]
    if in_delta is not None and in_delta > delta:
        print(
            f"(In sample shows {in_delta:+.3f} — larger, because v2 was fitted to those "
            "labels. Do not quote it.)"
        )

    print(f"\nreport     {base}/report/{scan_id}?version=2")
    print(f"dashboard  {base}/dashboard?scan_id={scan_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
