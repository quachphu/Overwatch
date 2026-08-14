# SPECS.md — Overwatch

Full technical specification. `CLAUDE.md` holds the rules; this holds the what.

---

## 1. Problem and thesis

AI coding tools made building apps cheap and made *trusting* them expensive. Automated QA can tell you a button throws a 500. It cannot tell you whether a finding is real, whether a user would care, or which of twenty-three findings is the one that loses you a customer. That is judgment, and judgment is what Terac sells as an API.

Overwatch is a company with no employees that sells verified QA. It hires humans per job, pays them on verified completion, and uses their judgment to make its own product measurably better.

**Deliberate scope choice:** we do not fix the customer's app — we do not have their repo, and any demo that claims otherwise is staged. **The thing that improves is our report.** This is honest, measurable, and works on a URL a judge picks live.

---

## 2. The improvement experiment

This is the core of the submission. Build everything else around it.

| | |
|---|---|
| **Baseline (v1)** | Raw ranked findings from the bug source. Noisy: false positives, wrong severity order. |
| **Human input** | Terac round 1. Participants view evidence bundles and answer *is this a real failure?* + *how bad, 1–5?* |
| **Intervention** | Recalibrate triage: few-shot the classifier with confirmed/rejected exemplars, re-weight ranking by mean human severity, drop finding categories with <30% confirmation. |
| **After (v2)** | Same raw findings, new triage. |
| **Fresh judgment** | Terac round 2. A **new** panel (enforced by filter) sees v1 and v2 side by side in randomized order and picks the more useful report. |

**Primary metric:** `preference_share` for v2 from round 2's fresh panel, with a Wilson 95% interval. This is the independent measurement — the panel never saw round 1 and had no hand in building v2.

**Supporting metric:** **held-out** `precision@k` — of the top `k` ranked findings, the fraction confirmed real by a majority of 3 raters, scored *only on labels that v2 was not built from.*

> The held-out qualifier is not a detail. v2 is fitted to the round-1 labels, so scoring it against those same labels measures the fitting procedure: on pure coin-flip labels, in-sample `precision@10` rises 0.50 → 1.00 in 400 of 400 simulated trials. The labels are therefore split by finding, v2 is rebuilt from one half, and both rankings are scored on the other. See docs/DECISIONS.md 010 and RESEARCH.md §12.5.

Report both. State `n`. State that round 2 excluded round-1 participants.

---

## 3. Architecture

### 3.1 Agents (Band)

Five agents, five OS processes, five Band registrations. Coordination happens **only** through the Band room — no direct function calls between them.

| Agent | Owns | Emits |
|---|---|---|
| **Scout** | Runs `BugSource.scan(url)`, writes evidence bundles | `@Triage <n> findings, <k> low-confidence` |
| **Triage** | Scores and ranks, selects findings needing human review | `@Recruiter verify these <k>, budget <p> participants` |
| **Recruiter** | All Terac interaction: builds opportunity, launches, reads results | `@Triage labels in: <summary>` |
| **Bursar** | Whop checkout, watches `payment.succeeded`, releases report | `@Scout paid scan for <url> — go` |
| **Critic** | Veto. Blocks release on low confirmation rate or PII in evidence | `BLOCKED: <reason>` |

**Runtime recruit path:** if Scout reports an auth wall, Triage calls `band_lookup_peers` → `band_add_participant` to bring in an **AuthProbe** agent that was not in the room. This is one of the three delete-test signals.

**Delete test answer, one sentence:** *Remove Band and the pipeline halts after Scout — the Recruiter's Terac opportunity is constructed from what Triage found, and there is no other channel between them.*

**Loop guard:** hard cap of 40 messages per room; each prompt forbids mentioning the same agent twice on one case without new evidence.

### 3.2 Bug source (pluggable)

```python
class BugSource(Protocol):
    async def scan(self, url: str) -> list[RawFinding]: ...
```

- `ReplayQASource` — **primary.** Replay QA takes a URL, explores, writes its own Playwright tests, records sessions, returns bug reports with root cause. Programmatic access is likely sales-gated; confirm at the booth.
- `PlaywrightSource` — **fallback.** Own loop: Playwright chromium → `page.accessibility.snapshot()` (compact semantic tree; raw HTML blows context on any SPA) → LLM picks one action per step → execute → observe. Cap 12 steps per journey. Attach `console`, `pageerror`, and `response` listeners before starting; half of all real findings come from those, not from model judgment.

Decision gate: if no Replay API access by **11:00**, switch to Playwright and do not revisit.

### 3.3 Services

| Service | Where | Notes |
|---|---|---|
| Ingress API | Render Web Service, **paid instance** | scan intake, all webhooks, task pages, dashboard |
| Scan orchestration | Render Workflow (`@app.task`) | durable wait across the human loop; `timeout=7200, retries=3` |
| Browser execution | Superserve microVM | isolation matters — we load arbitrary URLs |
| Evidence store | persistent disk or object storage | **must produce public URLs** |
| DB | Render Postgres | findings, labels, webhook_events, orders |

---

## 4. Data model

```python
class RawFinding(BaseModel):
    id: str
    scan_id: str
    journey: str
    step_intent: str
    expected: str
    observed: str
    screenshot_before_url: str      # PUBLIC — Terac participants load these
    screenshot_after_url: str
    console_errors: list[str]
    failed_requests: list[str]      # "POST /api/checkout -> 500"
    source: Literal["replay", "playwright"]
    agent_severity: Literal["blocker", "major", "minor", "cosmetic"]
    agent_confidence: float

class HumanLabel(BaseModel):
    finding_id: str
    submission_id: str
    participant_id: str
    is_real: Literal["clear_yes", "probably", "no", "cant_tell"]
    severity: int                   # 1-5
    round: int

class Report(BaseModel):
    scan_id: str
    version: Literal[1, 2]
    ranked_finding_ids: list[str]

class WebhookEvent(BaseModel):      # dedup table
    event_id: str                   # X-Event-ID, PRIMARY KEY
    received_at: datetime
```

---

## 5. Terac integration

### 5.1 Lifecycle

`create project → create opportunity (draft) → launch → submissions arrive → approve`

Approval is when you are billed. `review_type: "auto_approve"` accepts automatically — take it. Manual review makes a human the bottleneck at 16:00, and the credit lost to junk is smaller than the time lost to reviewing.

### 5.2 Round 1 — verification

```json
{
  "title": "Did this web app actually fail? (3 min, screenshots only)",
  "project_id": "<PROJECT_ID>",
  "num_participants": 12,
  "business_type": "b2c",
  "expected_days_to_complete": 5,
  "filters": [
    { "multi_select--country": { "$in": ["US"] } },
    { "integer--age": { "$gte": 18, "$lte": 65 } },
    { "multi_select--language": { "$in": ["en"] } }
  ],
  "screening_questions": [
    { "key": "attn", "text": "What will you be comparing in this task?", "pick": "one",
      "answers": [
        { "text": "Two screenshots of a website", "qualify_logic": "must" },
        { "text": "Two audio clips", "qualify_logic": "reject" },
        { "text": "A printed receipt", "qualify_logic": "reject" }
      ]},
    { "key": "device", "text": "What are you using right now?", "pick": "one",
      "answers": [
        { "text": "Laptop or desktop", "qualify_logic": "may" },
        { "text": "Phone or tablet", "qualify_logic": "may" }
      ]}
  ],
  "cross_quotas": [
    { "label": "Desktop", "conditions": [{"screening_question":"device","answer":"Laptop or desktop"}], "target": 6, "quota_type": "minimum" },
    { "label": "Mobile",  "conditions": [{"screening_question":"device","answer":"Phone or tablet"}],  "target": 4, "quota_type": "minimum" }
  ],
  "tasks": [{
    "sequence": 1,
    "task_type": "survey",
    "review_type": "auto_approve",
    "task_url": "https://<host>/t/r1?pid={{participant_id}}",
    "duration_minutes": 3
  }]
}
```

**Batching is the credit-efficiency play.** 12 participants × 5 findings each = 60 judgments covering 20 findings at 3 raters. "Use of human input" is 25% of the score and explicitly rewards efficient signal. Surface this number in the dashboard and say it in the pitch.

### 5.3 Round 2 — fresh judging

Same shape, `num_participants: 35`, one forced-choice question, **plus**:

```json
{ "reference--has_not_taken_study": { "$eq": "<ROUND_1_OPPORTUNITY_ID>" } }
```

Without this, round-1 participants can judge their own work and the word "fresh" is false. Exact value format for `reference--` filters is an UNKNOWN — confirm at the booth.

Randomize v1/v2 side per participant and persist the assignment. A fixed order is order bias.

### 5.4 Delivery

Webhook receiver at `/hooks/terac`, HMAC-verified, dedup on `X-Event-ID`, ACK within 10s then process async. **Plus** a polling loop at 20s intervals against `GET /opportunities/{id}/submissions?status=approved` — at 16:00 there is no time to debug a missed delivery.

### 5.5 Participant join

`task_url` must carry `{{participant_id}}`, and the task page must persist it **before rendering anything**. Without it you cannot join a Terac submission to the finding it judged, and the entire dataset is unusable.

---

## 6. Task pages

Both are server-rendered, single page, no framework, mobile-legible. Participants are on phones as often as laptops.

**`/t/r1` — verification.** Shows journey name, expected, observed, two screenshots side by side, console error verbatim. Two questions per finding, five findings per participant. **Never give participants live access to the app under test** — it is slow, unsafe, inconsistent across devices, and turns a 3-minute task into fifteen.

**`/t/r2` — comparison.** Two reports side by side, order randomized, one forced choice, optional one-line why.

---

## 7. Metrics

```python
def precision_at_k(ranked_ids, summaries, k=10) -> float | None:
    """Confirmed = strict majority of raters answered clear_yes or probably.

    Unlabeled findings are dropped before truncating to k, so v1 and v2 are scored over the
    same judged pool and the same denominator. Returns None when nothing was labeled — which
    is not 0.0, because "we have no idea" is not "humans rejected these".
    """

def split_summaries(summaries, *, seed="holdout", eval_fraction=0.5):
    """Split labeled findings into (fit, eval). v2 is rebuilt from `fit` alone and both
    rankings are then scored on `eval`. Split by finding, not by rater: holding out individual
    raters would leak the finding's verdict through the ones that remain."""

def holdout_k(n_eval, k=10) -> int:
    """precision@k is rank-invariant once k >= n_judged, so the held-out k is half the eval
    set. Without this, held-out precision@10 over a 10-finding eval half reads exactly 0.000
    on signal as well as on noise."""

def average_precision(ranked_ids, summaries, k=10) -> float | None:
    """Normalised by min(R, k) where R is the confirmed count in the judged set. Normalising
    by hits *found* instead scores "one real bug ranked first, nine missed" as 1.0."""

def wilson_interval(successes, n, z=1.96) -> tuple[float, float]:
    """Report this next to preference_share. n=35 gives roughly ±16pp."""
```

Do not claim a win the interval does not support. A 70/30 split at n=35 is real; 55/45 is not.

Do not quote an in-sample precision figure. It is computed and displayed, labelled as a
diagnostic, because a judge should find it on the page with its caveat rather than discover it was
removed — but it is not evidence of anything.

---

## 8. Milestones and gates

Wall-clock. State current time and ahead/behind at each gate.

| Time | Milestone | Acceptance |
|---|---|---|
| **10:45** | **Smoke test launched** | 5-participant, 2-min Terac task live. Timer started. Nothing else matters until this is out. |
| 12:30 | **Spine** | ≥15 real findings with public screenshot URLs that load in incognito. **Gate: if not met, switch to a pre-scanned fallback app and move on.** |
| **13:30** | **Round 1 launched** | Hard deadline. Ship whatever findings exist. |
| 15:00 | **Crew live** | 5 Band agents in one room, `Emit.EXECUTION` on, one full handoff chain visible. Whop tested with a real $1 charge. |
| 16:00 | **Round 2 launched** | Hard deadline. Launch with partial labels if needed. |
| 17:30 | **Results** | preference share + CI, held-out precision v1 vs v2, dashboard renders. |
| 18:20 | **Video recorded** | Twice. |
| **18:45** | **LOCK** | Does not reopen. |

**Smoke-test branch logic:**
- fills < 45 min → full two-round plan
- 45–120 min → collapse round 2 into the tail of round 1 as a within-subject comparison
- nothing by 14:00 → pivot the human task to whatever Terac can deliver and say so plainly in the pitch

**Cut order under pressure:** Band → revenue → Superserve → Render Workflows. **Never cut the two Terac rounds.**

---

## 9. Security requirements — non-negotiable

| Threat | Requirement |
|---|---|
| **SSRF** | Resolve DNS, then reject private/loopback/link-local/metadata ranges **on the resolved IP**, not by string match — `spoof.example.com` can resolve to 127.0.0.1. https only. Superserve egress controls as second layer. |
| **PII in evidence** | Critic blocks any finding whose screenshot a vision check flags as containing personal data. These images go to strangers. |
| **Destructive actions** | Block submit on anything matching payment/delete/send patterns. Never enter real card numbers. |
| **Secrets** | `agent_config.yaml` holds five live Band keys — gitignored from commit zero. Env vars on Render. |
| **Target-site abuse** | 12 steps per journey max, respect 429s, self-rate-limit. |

---

## 10. UNKNOWNS — do not guess

Confirm at the 10:20 sponsor Q&A. Implement each behind a named constant with a `# UNKNOWN:` comment so correcting it is one edit.

| # | Question | Blocks |
|---|---|---|
| 1 | Is the 5-day `expected_days_to_complete` minimum waived for hackathon keys? | Round scheduling |
| 2 | Realistic minutes-to-first-completion for a 3-min general-population task? | The entire day's plan |
| 3 | Credit budget per team? | `num_participants` on both rounds |
| 4 | REST + webhooks, or MCP + polling? | Delivery architecture |
| 5 | Exact value format for `reference--has_not_taken_study`? | Round 2 freshness |
| 6 | Is feasibility pricing instant, or human-priced out of band? | Whether to use feasibility at all |
| 7 | "Best Use of Terac" criteria (announced 09:15) | Possible re-prioritization |
| 8 | Replay QA: API key, MCP, or UI only? | BugSource choice, 11:00 gate |
| 9 | Render Workflows enabled on hackathon credits? | Orchestration layer |
| 10 | Whop payout verification for a same-day account? | Revenue track viability |

---

## 11. Out of scope

Auth/accounts, multi-tenancy, billing beyond one-time checkout, retry frameworks, caching, non-English apps, mobile-only sites, native apps, fixing customer code, any React build step.
