"""Durable orchestration for one scan, on Render Workflows.

Why this exists at all: the full run spans a Playwright scan, a paid human round that takes
hours, a recalibration, and a second paid round. A single process holding that in memory loses
the whole experiment to one redeploy, and the two Terac rounds are the majority of the score.

Verified against the installed `render_sdk` 0.7.0 on 2026-08-14 by reading the package:

    Workflows(*, default_retry=None, default_timeout=None, default_plan=None)
    Workflows.task(func=None, *, name=None, retry=None, timeout_seconds=None, plan=None)
    Workflows.start() -> reads RENDER_SDK_MODE / RENDER_SDK_SOCKET_PATH
    Retry(max_retries, wait_duration_ms, backoff_scaling=1.5)
    TaskCallable -> "A callable that can be awaited to run as a subtask."

This file used the module-level `render_sdk.task` / `render_sdk.start` instead. Both still work,
but `render_sdk/__init__.py` labels them "Deprecated: use Workflows.task and Workflows.start()
instead", and they take `options=Options(...)` where the instance method takes `retry=` and
`timeout_seconds=` directly — so the two are not drop-in equivalents and mixing them is a
mistake waiting to happen. Migrated to the documented instance API.

`TaskCallable`'s docstring is the composition model: inside a task, `await other_task(...)` runs
it as a durable subtask. Each step below is separately retried and separately resumable.

**There is no durable sleep and no wait-for-external-event primitive.** The docs describe no way
for a task to block until a webhook arrives, which is why `scan_and_verify` deliberately ends
after launching round 1 rather than awaiting its submissions. Round 2 is a separate entry point.

Every step is idempotent on the pipeline side — `run_scan` returns existing findings instead of
re-scanning, and launching an already-active Terac opportunity returns 409 which the client
treats as success. That is what makes retrying any of these safe rather than double-charging us.
"""

from __future__ import annotations

import logging
from typing import Any

from render_sdk import Retry, Workflows

from app import pipeline

logger = logging.getLogger("overwatch.workflow")

app = Workflows()

# A scan drives a real browser against someone else's site: slow, and flaky for reasons that
# are usually transient. Retried with backoff.
SCAN_RETRY = Retry(max_retries=2, wait_duration_ms=10_000, backoff_scaling=2.0)

# Terac calls are cheap and fast, but a 429 or a blip must not lose the round.
TERAC_RETRY = Retry(max_retries=3, wait_duration_ms=5_000)


@app.task(retry=SCAN_RETRY, timeout_seconds=900)
async def scan(url: str, order_id: str | None = None) -> dict[str, Any]:
    """Register and run a scan. Writes report v1."""
    scan_id = pipeline.create_scan(url, order_id=order_id)
    findings = await pipeline.run_scan(scan_id)
    logger.info("scan %s produced %d findings", scan_id, len(findings))
    return {"scan_id": scan_id, "n_findings": len(findings)}


@app.task
async def prepare_verification(scan_id: str, num_participants: int | None = None) -> dict[str, Any]:
    """Choose findings and build the round-1 assignment. Spends nothing."""
    return pipeline.prepare_round1(scan_id, num_participants=num_participants)


@app.task(retry=TERAC_RETRY, timeout_seconds=120)
async def hire_verifiers(scan_id: str, num_participants: int | None = None) -> dict[str, Any]:
    """Launch the round-1 Terac opportunity. Spends real credit."""
    return await pipeline.launch_round1(scan_id, num_participants=num_participants)


@app.task
async def recalibrate(scan_id: str) -> dict[str, Any]:
    """Re-rank the same findings on the human labels. Writes report v2."""
    return pipeline.build_v2(scan_id)


@app.task(retry=TERAC_RETRY, timeout_seconds=120)
async def hire_comparators(scan_id: str, num_participants: int | None = None) -> dict[str, Any]:
    """Launch the round-2 fresh-panel comparison."""
    return await pipeline.launch_round2(scan_id, num_participants=num_participants)


@app.task
async def scan_and_verify(url: str, order_id: str | None = None) -> dict[str, Any]:
    """Scan, then hire humans to verify it.

    Stops here rather than continuing into round 2. Round 1 has to *finish* first — real people
    take hours — and its labels are the input to v2. Round 2 is started separately once enough
    submissions are approved, by Recruiter or by `POST /api/scans/{id}/round2`.
    """
    scanned = await scan(url, order_id)
    scan_id = scanned["scan_id"]

    if not scanned["n_findings"]:
        # A clean app is a real result. Paying twelve people to verify an empty list is not a
        # measurement, it is a donation.
        logger.info("scan %s found nothing; skipping the paid round", scan_id)
        return {**scanned, "round1": None, "skipped": "no findings to verify"}

    prepared = await prepare_verification(scan_id)
    launched = await hire_verifiers(scan_id)
    return {**scanned, "prepared": prepared, "round1": launched}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    # Registers the tasks or runs them, depending on RENDER_SDK_MODE.
    app.start()
