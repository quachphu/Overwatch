# DECISIONS.md

Append-only. Newest at the bottom. Never edit or delete an entry — supersede it with a new one.

**Why this file exists:** in a long session your context gets compacted and you lose *why* things were decided. Without this you will re-litigate settled questions at 15:00, or worse, quietly reverse a decision and break something upstream. Before proposing any architectural change, read this file. If your proposal contradicts an entry, say which one and argue against it explicitly.

**Format:**

```
## NNN — <decision>
Date/time · Status: accepted | superseded by NNN
Context: what forced the choice
Decision: what we're doing
Consequence: what this costs us, what it buys
```

---

## 001 — The thing that improves is our report, not the customer's app
2026-08-14 · accepted

**Context:** the obvious framing is "agent finds bugs, agent fixes app, show before/after." We cannot fix an arbitrary pasted URL — we don't have the repo. Demoing on a toy app we control reads as staged to judges.

**Decision:** the measured improvement is Overwatch's own report quality. Baseline v1 = raw ranked findings. Human labels recalibrate triage. v2 = same findings, better ranking. A fresh panel judges v1 vs v2.

**Consequence:** works on any URL including one a judge picks live. Costs us the more visceral "we fixed your app" demo moment. Maps directly onto the event's stated ask — prompt/retrieval changes driving a before/after judged by fresh humans.

## 002 — Replay QA is the bug source, not our own browser agent
2026-08-14 · accepted

**Context:** Replay QA already takes a URL, explores, writes its own Playwright tests, records sessions, and returns bug reports with root cause. That is 3–4 hours of our build, already built, with a booth at this event.

**Decision:** `BugSource` protocol with `ReplayQASource` primary and `PlaywrightSource` fallback. Decision gate at 11:00 — if no programmatic access by then, switch to Playwright and do not revisit.

**Consequence:** saves hours and wins the Replay track if access lands. Risk is that Replay's API appears sales-gated. The protocol boundary means switching costs one class, not a redesign.

## 003 — Terac participants never touch the app under test
2026-08-14 · accepted

**Context:** the intuitive task design is "here's the app, try it, tell us if it's broken."

**Decision:** participants see evidence bundles — journey description, expected vs observed, two screenshots, verbatim console error. Never a live app.

**Consequence:** task drops from ~15 min to 3 min, works identically on phone and laptop, no auth/state/security problems, and no risk of a participant taking a destructive action on someone else's app. Costs some fidelity: they judge our evidence, not the app itself. This is the right trade and we should say so if asked.

## 004 — Batch 5 findings per participant
2026-08-14 · accepted

**Context:** "Use of human input" is 25% of the score and explicitly rewards getting signal efficiently within the credit budget.

**Decision:** round 1 = 12 participants × 5 findings = 60 judgments covering 20 findings at 3 raters each.

**Consequence:** roughly a third the cost of one-finding-per-participant. Surface the number in the dashboard and say it out loud in the pitch. Risk is rater fatigue by finding 5 — randomize order within the set.

## 005 — auto_approve on Terac tasks
2026-08-14 · accepted

**Context:** manual review means a human approves each submission before we're billed and before we get data.

**Decision:** `review_type: "auto_approve"`.

**Consequence:** we pay for some junk submissions. We accept that — at 16:00 the bottleneck is us, not credits. Attention-check screener plus dropping raters whose answers are all identical is the quality gate instead.

## 006 — Round 2 must exclude round 1 participants
2026-08-14 · accepted

**Context:** the claim "judged by a fresh round of humans" is only true if it's enforced.

**Decision:** `reference--has_not_taken_study` filter on round 2, excluding round 1's opportunity ID. If the filter cannot be constructed, Recruiter blocks rather than launching.

**Consequence:** a judge who builds eval environments for a living will look for exactly this hole. Exact value format is UNKNOWN — confirm at booth.

## 007 — Webhooks AND polling, both
2026-08-14 · accepted

**Context:** webhooks are lower latency; polling is debuggable.

**Decision:** run both. Webhook receiver with HMAC verification and `X-Event-ID` dedup, plus a 20s polling loop against `submissions?status=approved`.

**Consequence:** slight duplication, handled by the dedup table. Buys us the ability to not care when a single delivery goes missing at 16:00, which is the failure we cannot afford to debug.

## 008 — Five separate Band agents, not one process with five personas
2026-08-14 · accepted

**Context:** it is much easier to run one process that switches system prompts.

**Decision:** five registrations, five UUIDs, five keys, five processes.

**Consequence:** more setup and five terminals. But Band's guide explicitly names "one process switching personas" as a thing that reads as collaboration and isn't — a single participant does all the talking and it's visible in the room. This is the difference between winning and not winning the Band track.

## 009 — Server-rendered pages, no React build
2026-08-14 · accepted

**Context:** two task pages and a dashboard.

**Decision:** FastAPI + Jinja templates.

**Consequence:** no build step, no node toolchain, no bundler failure at 17:00. Task pages must be mobile-legible — participants are on phones as often as laptops.

---

## 010 — The reported precision must be held out, not in-sample
2026-08-14 14:00 · accepted

**Context:** `pipeline.results` scored report v2 against the same round-1 labels that
`triage.rank_v2` was fitted to. Because v2 promotes confirmed findings and multiplies rejected
ones by 0.15, this measured our fitting procedure rather than the ranking. Simulated with **pure
coin-flip labels**, in-sample `precision@10` went 0.50 → 1.00 in **400 of 400** trials — a
50-point "win" from labels containing no information whatsoever.

**Decision:** split the round-1 labels in half **by finding**; rebuild v2 from the fit half only;
score both rankings on the eval half neither has seen. That held-out figure is the reported
number, and it is what the dashboard leads with. The in-sample figure stays visible under
"In-sample diagnostic — not evidence". Rankings are also condensed to the judged pool before
truncation to `k`, so v1 and v2 share one denominator.

Splitting by finding rather than by rater is deliberate: the unit v2 is fitted on is the
finding-level verdict, so holding out individual raters would leak that verdict through the
raters who remain.

**Consequence:** the headline number gets smaller and can now come out flat or negative — on the
current seed fixture it is a tie rather than +0.10. That is the point; a metric that cannot fail
is not evidence. Costs us half the labels' statistical power for the ranking claim, which is the
right price for a claim that survives the question "did you evaluate on your training labels?".
The customer still receives the full-label v2, since for them using every label is strictly
better; the split is an evaluation device, not a product change. Full detail: `RESEARCH.md` §12.5.

## 011 — `pyproject.toml` for tooling, `requirements.txt` for installing
2026-08-14 14:10 · accepted

**Context:** professionalizing the repo wanted modern packaging, but the deploy path, the
`Makefile` and CI all run `pip install -r requirements.txt`, and that file carries hard-won exact
pins plus comments explaining which are forced by transitive dependencies.

**Decision:** `pyproject.toml` holds project metadata and all tool config (ruff, pytest) and
mirrors the dependency list. `requirements.txt` stays the installer of record. `pytest.ini` was
deleted so pytest has exactly one config.

**Consequence:** the two dependency lists can drift, which is a real cost and the reason each
points at the other. Rejected the alternative of moving installs to `pip install -e .` mid-build:
switching the install path on the day of a hackathon risks discovering a packaging difference
under time pressure, for no benefit a judge will ever see.

## 012 — Authenticate the endpoints that spend money, keyed to reachability
2026-08-14 14:05 · accepted

**Context:** `POST /api/scans/{id}/round1` hires twelve people through Terac. It was
unauthenticated, and it is not idempotent — every call creates a new opportunity. Anyone able to
reach the service and see a scan id could loop it against our balance. `/round2`, `/v2`, `/poll`
and `/api/terac/balance` were equally open.

**Decision:** an `OPERATOR_TOKEN` shared secret, compared with `secrets.compare_digest`, required
when `PUBLIC_BASE_URL` is a public host. On a public host with no token configured those routes
return **503** rather than running. On localhost no token is needed.

**Consequence:** an instance on Render cannot serve money-spending endpoints unauthenticated even
if someone forgets to set the variable, and `make rehearse` and the demo stay frictionless on a
laptop. Rejected keying this to a `DEBUG` flag: a flag can be wrong, whereas the public base URL
is the same value we hand Terac as the participant task host, so if it is real then strangers can
reach us by definition. `/api/scan` deliberately stays open as the product ingress.

---

<!-- Append new decisions below. Include time of day; it matters for reconstructing what we knew when. -->
## 013 — Safety re-verification happens at the moment of action, not at planning time (14:35)

**Context:** the destructive-action guard screened an element at DOM index *i*, then clicked
`elements[i]` from a freshly re-queried list. Reproduced in a real browser: after a two-button
cookie banner unmounts, the index screened as `About us` *is* `Delete account`.

**Decision:** `_safe_targets` returns a `safety_key` fingerprinting every input the safety decision
was based on (all four label sources plus the href). It is re-derived immediately before the click
and compared; any mismatch skips the step. `is_destructive` is called on the *union* of those
sources, untruncated, plus the href.

**Consequence:** a shifted index costs one skipped step instead of a stranger's data, and the
evidence bundle can no longer describe an action that did not happen. Rejected the
`page.get_by_role(name=...)` locator alternative for now — it eliminates the bug class rather than
patching it and is the right long-term shape, but it is a larger change to a file we are not
otherwise restructuring today. Recorded so the next person knows it was considered.

---

## 014 — We never show a success page for work we did not store (14:50)

**Context:** `POST /t/r1` rendered "That's everything. Thank you." with zero labels stored whenever
the assignment lookup missed, and one malformed `severity` rolled back every good label in the same
submission.

**Decision:** a submission we cannot attribute returns **409** with a page that says the failure is
ours, tells the participant not to resubmit, and promises payment for time already spent. Each label
is validated individually through `HumanLabel`; a bad row is skipped and the count is surfaced on
the thank-you page. Round-2 votes require an assignment and a `left_version` that matches it.

**Consequence:** we can no longer take someone's work, store nothing, and thank them for it — the
single most corrosive thing this system could do, since the entire premise is that human judgment is
worth paying for. Cost: a participant who hits a genuine server fault sees an error rather than a
polite lie, and we handle it by hand.

This change immediately exposed a real bug in `scripts/rehearse_experiment.py`, which had been
posting a verdict outside the `IsReal` vocabulary (RESEARCH.md §13.8). Validation earning its keep
within minutes is the argument for putting it at the boundary rather than trusting callers.

---

## 015 — Launching a paid round is idempotent, keyed on the opportunity id (15:05)

**Context:** `launch_round1`/`launch_round2` hired a panel unconditionally, while every caller above
them retries — `max_retries=2` on the Render task, a re-promptable Recruiter, and a double-clickable
operator POST.

**Decision:** `_launched_round` returns the existing round when a **launched opportunity id** is
present, and the launch functions return it instead of creating a second one. Participant counts are
bounded by `MAX_PARTICIPANTS_PER_ROUND = 60`, and a non-positive count is refused rather than
coerced.

**Consequence:** a retry is free instead of doubling the bill. Keyed on the opportunity id rather
than the existence of a `Round` row on purpose: a run that died between inserting the row and
creating the opportunity is a case we *want* to be retryable. Refusing zero rather than defaulting
it means an agent that miscalculates gets an error instead of quietly hiring twelve people.

---

## 016 — We refuse to pay for judgment on evidence that does not exist (15:15)

**Context:** `ReplayQASource` emits findings with no screenshots and is documented as the preferred
source. `is_public_base()` inspects a URL string, so it cannot see missing files, and the launch
passed. Separately, evidence URLs are absolute and frozen at capture time, so a scan run before
`PUBLIC_BASE_URL` was public has `localhost` baked into the rows — which that same check cannot see.

**Decision:** `prepare_round1` drops findings with no screenshot and raises if that empties the set;
`launch_round1` also validates the **stored** URLs, not just the current setting.

**Consequence:** a source that cannot produce evidence fails loudly at launch instead of silently
converting a paid round into unusable labels. Chose to validate stored URLs rather than refactor
evidence storage to relative paths: the refactor is cleaner and touches storage, templates and the
report renderer, and this is the same guarantee for one function's worth of change.
