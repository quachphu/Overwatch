"""`PlaywrightSource` — the fallback bug source, and the default. SPECS.md §3.2.

Design notes that matter:

* **Listeners are attached before the first navigation.** `console`, `pageerror` and
  `response` produce roughly half of all real findings; a finding backed by
  `POST /api/checkout -> 500` needs no model judgment at all. If the listeners go on after
  `goto`, the load-time errors — the most valuable ones — are already gone.
* **The accessibility snapshot, not the HTML.** `page.accessibility.snapshot()` is a compact
  semantic tree. Raw HTML on any SPA blows the context window and buys nothing.
* Hard cap: `max_steps_per_journey` (12) bounds the clicks per journey. There are two journeys,
  both defined here, so this is the cost control and the target-site abuse control from
  SPECS.md §9. (A `max_journeys` setting used to be read here and never applied — a documented
  guard that does not exist is worse than an absent one, because it stops anyone looking.)
* Destructive controls are never clicked. `app.security.is_destructive` is deliberately
  over-broad: a false positive costs one skipped click, a false negative deletes a
  stranger's data.

Findings are derived from **observed browser signals**, not from asking a model whether a
page looks broken. A console error is evidence; a model's opinion is not, and the whole
point of Round 1 is to have humans adjudicate what the machine could not.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from app.config import settings
from app.models import RawFinding, new_id
from app.security import assert_safe_target, is_destructive
from app.sources.evidence import save_screenshot

logger = logging.getLogger(__name__)

# Status codes that are evidence of a server-side failure. 4xx on a resource the page asked
# for is a real defect; 401/403 are excluded because they usually mean "not logged in",
# which is an auth wall rather than a bug.
FAILING_STATUSES = {500, 502, 503, 504, 400, 404, 405, 409, 422}
IGNORED_STATUSES = {401, 403}

# Noise that appears on a large share of the public web and tells us nothing about the app.
CONSOLE_NOISE = (
    "favicon",
    "third-party cookie",
    "google analytics",
    "gtag",
    "facebook.net",
    "doubleclick",
    "hotjar",
    "sentry",
    "preload",
    "was preloaded using link preload",
    "deprecatedapi",
    "devtools",
)


SAFE_TARGET_SELECTOR = "a[href], button, [role=button], input[type=submit]"

# Schemes we never activate. `javascript:` is the dangerous one — it does not start with
# "http", so an origin check based on prefixes waves it through while it runs arbitrary code
# in the page, destructive or not.
_BLOCKED_HREF_SCHEMES = {"javascript", "data", "blob", "mailto", "tel", "sms", "file"}

# Playwright's own failure modes. A control covered by an overlay, or a handle invalidated by a
# re-render, says nothing about the app under test — and must never be sold as a finding.
TOOLING_ERROR_MARKERS = (
    "Timeout",
    "not attached",
    "not visible",
    "intercepts pointer events",
    "element is not enabled",
    "Target closed",
    "Execution context was destroyed",
)


# Below the workflow's own `timeout_seconds=900` so the deadline fires here, in-process, where
# the `finally` can still close the browser and the findings so far can still be returned.
SCAN_DEADLINE_SECONDS = 600


def _dedupe(findings: list[RawFinding]) -> list[RawFinding]:
    """Collapse findings that share a root cause, recording how many pages showed it.

    Without this, one 404 on a site-wide stylesheet became a finding on the load journey plus
    one per interaction step — up to 13 rows describing a single bug. `prepare_round1`'s budget
    is ~20 findings at 3 raters each, so most of the 60 paid judgments went on re-answering the
    same question, and `confirmation_rate_by_category` then decided whether to drop a whole
    category based on one duplicated root cause.

    The count is kept and surfaced: "seen on 13 pages" is itself severity evidence for the
    human, which is strictly more useful than 13 separate tasks.
    """
    first: dict[tuple[str, str], RawFinding] = {}
    counts: dict[tuple[str, str], int] = {}

    for finding in findings:
        # The signature is the *cause* — the first concrete error string — not the step that
        # happened to reveal it, so the same error found via different clicks collapses.
        evidence = [*finding.failed_requests, *finding.console_errors]
        cause = (evidence[0] if evidence else finding.observed)[:200]
        key = (finding.category, cause)
        counts[key] = counts.get(key, 0) + 1
        if key not in first:
            first[key] = finding

    out: list[RawFinding] = []
    for key, finding in first.items():
        seen = counts[key]
        if seen > 1:
            finding = finding.model_copy(
                update={
                    "observed": (
                        f"{finding.observed} (the same failure occurred on {seen} of the "
                        f"{len(findings)} pages checked)"
                    )
                }
            )
        out.append(finding)
    return out


def _origin_of(url: str) -> str:
    """`scheme://host:port` for `url`, or "" if it has none."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _display_label(sources: list[str | None]) -> str:
    """The shortest human-readable name for a control, for evidence text."""
    for raw in sources:
        if raw and raw.strip():
            return " ".join(raw.split())[:80]
    return ""


def _is_destructive_target(sources: list[str | None], href: str | None) -> bool:
    """Whether any label source or the href suggests activating this destroys something.

    Screens the *union*, untruncated, because `is_destructive` is only as good as what it is
    shown: a false positive costs one skipped click, a false negative deletes a stranger's
    data. Deliberately asymmetric.
    """
    haystack = " ".join(s for s in [*sources, href] if s)
    return is_destructive(haystack)


def _safety_key(sources: list[str | None], href: str | None) -> str:
    """A fingerprint of everything the safety decision was based on.

    Re-derived immediately before the click and compared. Any DOM change that would move a
    different element under the same index changes this string, so a stale positional index
    can no longer smuggle an unscreened control past `_is_destructive_target`.
    """
    return "\x1f".join((s or "").strip() for s in [*sources, href])


@dataclass
class _Signals:
    """Everything the browser told us during one step."""

    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)

    def clear(self) -> None:
        self.console_errors.clear()
        self.page_errors.clear()
        self.failed_requests.clear()

    def snapshot(self) -> _Signals:
        return _Signals(
            console_errors=list(self.console_errors),
            page_errors=list(self.page_errors),
            failed_requests=list(self.failed_requests),
        )

    @property
    def empty(self) -> bool:
        return not (self.console_errors or self.page_errors or self.failed_requests)


def _is_noise(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in CONSOLE_NOISE)


def _classify(signals: _Signals) -> tuple[str, str, float]:
    """Map observed signals to `(category, severity, confidence)`.

    Confidence is the machine's *own* certainty and is deliberately conservative for
    console-only findings — those are precisely the ones Triage should spend human budget
    on (docs/AGENTS.md: "Select the findings where your own confidence is weakest").
    """
    server_errors = [r for r in signals.failed_requests if " -> 5" in r]
    client_errors = [r for r in signals.failed_requests if " -> 4" in r]

    if server_errors:
        return "server_error", "blocker", 0.92
    if signals.page_errors:
        return "unhandled_exception", "major", 0.78
    if client_errors:
        return "failed_request", "major", 0.55
    if signals.console_errors:
        return "console_error", "minor", 0.35
    return "anomaly", "cosmetic", 0.2


class PlaywrightSource:
    """Own exploration loop over Playwright chromium."""

    name = "playwright"

    def __init__(
        self,
        *,
        max_steps: int | None = None,
        headless: bool = True,
    ) -> None:
        # `or` treated 0 as absent, so `max_steps=0` — the natural spelling for "load the page,
        # touch nothing", which is what you reach for on a sensitive target — performed twelve
        # clicks on a stranger's production site instead of none.
        self.max_steps = settings.max_steps_per_journey if max_steps is None else max(0, max_steps)
        self.headless = headless

    async def available(self) -> bool:
        try:
            import playwright  # noqa: F401
            from playwright.async_api import async_playwright  # noqa: F401
        except Exception:
            logger.warning("Playwright is not importable — run `playwright install chromium`.")
            return False
        return True

    async def scan(self, url: str, scan_id: str) -> list[RawFinding]:
        """Explore `url` and return findings. Never raises on an unproductive scan."""
        target = assert_safe_target(url)

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("Playwright unavailable; returning no findings.")
            return []

        findings: list[RawFinding] = []
        signals = _Signals()

        async with async_playwright() as pw:
            browser = None
            try:
                # Inside the try: `pip install playwright` succeeds without
                # `playwright install chromium`, and `available()` only checks the import, so a
                # missing browser binary raised straight out of this method and broke the
                # protocol's "must not raise on an unproductive scan" contract.
                browser = await pw.chromium.launch(headless=self.headless)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    ignore_https_errors=False,
                    # Otherwise a click on a link to a large file streams it to a temp dir.
                    accept_downloads=False,
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 Overwatch-QA/1.0"
                    ),
                )
                page = await context.new_page()

                # ── Listeners BEFORE the first navigation. See module docstring. ──
                def on_console(msg: Any) -> None:
                    if msg.type == "error" and not _is_noise(msg.text):
                        signals.console_errors.append(msg.text[:500])

                def on_page_error(exc: Any) -> None:
                    text = str(exc)
                    if not _is_noise(text):
                        signals.page_errors.append(text[:500])

                def on_response(response: Any) -> None:
                    status = response.status
                    if status in IGNORED_STATUSES or status not in FAILING_STATUSES:
                        return
                    signals.failed_requests.append(
                        f"{response.request.method} {response.url[:200]} -> {status}"
                    )

                page.on("console", on_console)
                page.on("pageerror", on_page_error)
                page.on("response", on_response)

                # Worst-case per-step wall clock exceeded the workflow's own 900s timeout, so
                # the task was killed before `run_scan` persisted anything and the retry
                # re-scanned the site from scratch. Timing out in-process instead lets the
                # `finally` close the browser and returns the findings gathered so far.
                async def journeys() -> None:
                    findings.extend(await self._journey_load(page, target, scan_id, signals))
                    findings.extend(await self._journey_interact(page, target, scan_id, signals))

                try:
                    await asyncio.wait_for(journeys(), timeout=SCAN_DEADLINE_SECONDS)
                except TimeoutError:
                    logger.warning(
                        "Scan of %s hit the %ds deadline; returning %d findings found so far.",
                        target,
                        SCAN_DEADLINE_SECONDS,
                        len(findings),
                    )
            except Exception as exc:
                # A crashed scan still returns whatever it found. Scout reports the blocker.
                logger.exception("Scan of %s failed mid-flight: %s", target, exc)
            finally:
                if browser is not None:
                    await browser.close()

        deduped = _dedupe(findings)
        logger.info(
            "PlaywrightSource produced %d findings for %s (%d before de-duplication)",
            len(deduped),
            target,
            len(findings),
        )
        return deduped

    # ── journeys ─────────────────────────────────────────────────────────────────────

    async def _shot(self, page: Any, scan_id: str, label: str) -> str:
        try:
            data = await page.screenshot(full_page=False)
        except Exception as exc:
            logger.warning("Screenshot failed (%s): %s", label, exc)
            return ""
        return save_screenshot(scan_id, f"{label}-{new_id('img')}.png", data)

    async def _journey_load(
        self, page: Any, url: str, scan_id: str, signals: _Signals
    ) -> list[RawFinding]:
        """Journey 1 — just load the page. Cheap and productive."""
        signals.clear()
        before = ""
        try:
            before = await self._shot(page, scan_id, "load-before")
        except Exception as exc:
            # `_shot` swallows Playwright errors itself, so reaching here means the *write*
            # failed — a full or read-only evidence disk. That is worth a line in the log,
            # because the visible symptom is only a report full of missing images.
            logger.warning("Could not store the pre-load screenshot: %s", exc)

        status_code: int | None = None
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            status_code = response.status if response else None
            await page.wait_for_timeout(2_500)
        except Exception as exc:
            after = await self._shot(page, scan_id, "load-after")
            return [
                RawFinding(
                    scan_id=scan_id,
                    journey="Initial page load",
                    step_intent=f"Open {url} and wait for the DOM to be ready",
                    expected="The page loads and renders without error",
                    observed=f"Navigation failed: {str(exc)[:300]}",
                    screenshot_before_url=before,
                    screenshot_after_url=after,
                    console_errors=list(signals.console_errors),
                    failed_requests=list(signals.failed_requests),
                    source="playwright",
                    agent_severity="blocker",
                    agent_confidence=0.95,
                    category="navigation_failure",
                )
            ]

        after = await self._shot(page, scan_id, "load-after")
        out: list[RawFinding] = []

        if status_code and status_code >= 400:
            out.append(
                RawFinding(
                    scan_id=scan_id,
                    journey="Initial page load",
                    step_intent=f"Open {url}",
                    expected="The server returns a success status for the landing page",
                    observed=f"The server returned HTTP {status_code}",
                    screenshot_before_url=before,
                    screenshot_after_url=after,
                    console_errors=list(signals.console_errors),
                    failed_requests=list(signals.failed_requests),
                    source="playwright",
                    agent_severity="blocker",
                    agent_confidence=0.9,
                    category="http_error",
                )
            )

        if not signals.empty:
            category, severity, confidence = _classify(signals)
            out.append(
                self._finding_from_signals(
                    scan_id=scan_id,
                    journey="Initial page load",
                    step_intent=f"Open {url} and observe browser diagnostics",
                    expected="The page loads with no console errors and no failed requests",
                    signals=signals.snapshot(),
                    before=before,
                    after=after,
                    category=category,
                    severity=severity,
                    confidence=confidence,
                )
            )
        return out

    async def _journey_interact(
        self, page: Any, url: str, scan_id: str, signals: _Signals
    ) -> list[RawFinding]:
        """Journey 2 — click safe interactive elements one at a time, observing after each.

        One action per step, screenshot before and after, so every finding ships with the
        two images the Terac task page shows side by side.
        """
        out: list[RawFinding] = []
        try:
            candidates = await self._safe_targets(page)
        except Exception as exc:
            logger.warning("Could not enumerate interactive elements: %s", exc)
            return out

        for index, candidate in enumerate(candidates[: self.max_steps]):
            label = candidate["label"]
            selector_index = candidate["index"]
            signals.clear()

            before = await self._shot(page, scan_id, f"step{index}-before")
            try:
                elements = await page.query_selector_all(SAFE_TARGET_SELECTOR)
                if selector_index >= len(elements):
                    continue
                element = elements[selector_index]

                # Re-verify the safety decision against the element we are about to click.
                # `_safe_targets` screened the element at this index in an *earlier* DOM
                # snapshot; a single dismissed cookie banner shifts every later index, so
                # without this the scanner could click a control whose label was never screened
                # while having logged "Skipping destructive control: Delete account". It also
                # kept the *old* label in the evidence, so humans were paid to adjudicate an
                # action that never happened.
                current = [
                    await element.get_attribute("aria-label"),
                    await element.inner_text(),
                    await element.get_attribute("title"),
                    await element.get_attribute("value"),
                ]
                current_href = await element.get_attribute("href")
                if _safety_key(current, current_href) != candidate["safety_key"]:
                    logger.info(
                        "Skipping step %d: the DOM shifted under index %d (%r is now %r).",
                        index,
                        selector_index,
                        label,
                        _display_label(current),
                    )
                    continue
                if _is_destructive_target(current, current_href):
                    logger.info("Skipping step %d: %r is destructive on re-check.", index, label)
                    continue

                await element.click(timeout=5_000)
                await page.wait_for_timeout(1_800)
            except Exception as exc:
                # Tool-side failures are not defects in the customer's app. Selling a human
                # "The interaction failed: Timeout 5000ms exceeded" wastes a paid judgment on
                # our own bug — and because `select_for_review` orders by distance from 0.5,
                # confidence 0.3 put these *ahead* of genuine 5xx findings in the queue.
                text = str(exc)
                if any(marker in text for marker in TOOLING_ERROR_MARKERS):
                    # Playwright's actionability call log (why the click never fired — covered
                    # by an overlay, not stable, off-screen) lives past character 160; the old
                    # truncation cut it off before the useful part, hiding the real reason.
                    logger.info("Step %d skipped, tooling error: %s", index, text[:600])
                    continue
                after = await self._shot(page, scan_id, f"step{index}-after")
                out.append(
                    RawFinding(
                        scan_id=scan_id,
                        journey=f"Interact with '{label}'",
                        step_intent=f"Click the control labelled '{label}'",
                        expected="The control responds and the page reaches a usable state",
                        observed=f"The interaction failed: {text[:250]}",
                        screenshot_before_url=before,
                        screenshot_after_url=after,
                        console_errors=list(signals.console_errors),
                        failed_requests=list(signals.failed_requests),
                        source="playwright",
                        agent_severity="minor",
                        agent_confidence=0.3,
                        category="interaction_failure",
                    )
                )
                continue

            after = await self._shot(page, scan_id, f"step{index}-after")

            if not signals.empty:
                category, severity, confidence = _classify(signals)
                out.append(
                    self._finding_from_signals(
                        scan_id=scan_id,
                        journey=f"Interact with '{label}'",
                        step_intent=f"Click the control labelled '{label}' and observe the result",
                        expected="Clicking this control produces no errors and no failed requests",
                        signals=signals.snapshot(),
                        before=before,
                        after=after,
                        category=category,
                        severity=severity,
                        confidence=confidence,
                    )
                )

            # Return to the entry point so each step is independent and one navigation
            # cannot strand the rest of the journey.
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(700)
            except Exception:
                break

            # Self-rate-limit. SPECS.md §9 target-site abuse control.
            await asyncio.sleep(0.4)

        return out

    async def _safe_targets(self, page: Any) -> list[dict[str, Any]]:
        """Interactive elements that are safe to activate.

        Uses the accessibility tree for labels where possible and skips anything
        `is_destructive` flags, anything offscreen, and anything that leaves the origin.
        """
        elements = await page.query_selector_all(SAFE_TARGET_SELECTOR)
        # `page.evaluate` accepts no timeout and runs on the page's JS thread, so a target that
        # blocks that thread hung the whole scan here with nothing to interrupt it. `page.url`
        # is read from the driver, not the page.
        origin = _origin_of(page.url)

        out: list[dict[str, Any]] = []
        for index, element in enumerate(elements):
            try:
                if not await element.is_visible():
                    continue
                href = await element.get_attribute("href")

                # Screen the union of every label source, not the first non-empty one. The old
                # `or` chain short-circuited, so `<button aria-label="cta-42">Delete account</button>`
                # was screened as "cta-42" and clicked. `value` is included because
                # `input[type=submit]` has no inner text — those elements were previously
                # unlabelled and so never clicked at all.
                sources = [
                    await element.get_attribute("aria-label"),
                    await element.inner_text(),
                    await element.get_attribute("title"),
                    await element.get_attribute("value"),
                ]
                label = _display_label(sources)
                if not label:
                    continue

                # Screen the href too, untruncated. A destructive word can sit past character
                # 80 of a long label, and `\b` boundaries make `/items/42/delete` match — GET
                # routes that delete are still common on admin surfaces.
                if _is_destructive_target(sources, href):
                    logger.info("Skipping destructive control: %s", label)
                    continue

                if href:
                    scheme = href.split(":", 1)[0].lower() if ":" in href else ""
                    if scheme in _BLOCKED_HREF_SCHEMES:
                        continue
                    # Compare parsed origins. `startswith(origin)` treated
                    # `https://example.com.evil.com` as same-origin and let `//evil.com` past
                    # entirely, either of which leaves the target vetted by assert_safe_target.
                    target_origin = _origin_of(urljoin(page.url, href))
                    if target_origin and origin and target_origin != origin:
                        continue
                out.append(
                    {"index": index, "label": label, "safety_key": _safety_key(sources, href)}
                )
            except Exception as exc:
                # Expected during normal operation: the DOM can re-render between locating an
                # element and reading it, which invalidates the handle. Skipping is correct —
                # and note it skips *before* the element is ever interacted with, so a failure
                # here can never bypass the `is_destructive` check above. Logged at debug so a
                # page that yields no candidates at all can still be explained.
                logger.debug("Skipped candidate %d, could not inspect it: %s", index, exc)
                continue

        # De-duplicate by label so twenty identical nav links do not consume the budget.
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for item in out:
            if item["label"].lower() in seen:
                continue
            seen.add(item["label"].lower())
            unique.append(item)
        return unique

    @staticmethod
    def _finding_from_signals(
        *,
        scan_id: str,
        journey: str,
        step_intent: str,
        expected: str,
        signals: _Signals,
        before: str,
        after: str,
        category: str,
        severity: str,
        confidence: float,
    ) -> RawFinding:
        parts: list[str] = []
        if signals.failed_requests:
            parts.append("Failed requests: " + "; ".join(signals.failed_requests[:3]))
        if signals.page_errors:
            parts.append("Uncaught exception: " + signals.page_errors[0])
        if signals.console_errors:
            parts.append("Console error: " + signals.console_errors[0])

        return RawFinding(
            scan_id=scan_id,
            journey=journey,
            step_intent=step_intent,
            expected=expected,
            observed=" | ".join(parts)[:1000] or "Unexpected browser diagnostics",
            screenshot_before_url=before,
            screenshot_after_url=after,
            console_errors=signals.console_errors + signals.page_errors,
            failed_requests=signals.failed_requests,
            source="playwright",
            agent_severity=severity,  # type: ignore[arg-type]
            agent_confidence=confidence,
            category=category,
        )
