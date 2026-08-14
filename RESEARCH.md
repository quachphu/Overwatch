# RESEARCH.md — Phase 0

Research pass performed **2026-08-14**, structure per `docs/KICKOFF.md`.

Every claim carries the URL it came from. Where I could not fetch a page or the docs are silent,
it says so. **"docs say" vs "I infer"** is marked explicitly throughout.

**The single most valuable artifact found:** the live OpenAPI 3.0.3 spec at
<https://terac.com/api/external/v2/openapi.json> (145 KB). It is not linked from the guides index
(`/docs/developers/api-reference` is a 404) but it is public and authoritative. A local copy is at
`docs/terac_openapi.json`. Every Terac field name in this repo was read from it.

---

## 1. Terac object model

Base URL `https://terac.com/api/external/v2` — confirmed by both
<https://terac.com/docs/developers/guides> and the spec's own `servers[0].url`.
Auth: `Authorization: Bearer <key>`, scoped per organization
(<https://terac.com/docs/developers/guides/authentication>). Rate limit **100 req/min per key**,
exceeding returns `429 RATE_LIMITED` (same page).

### 1.1 Lifecycle

Two launch paths exist. `SPECS.md` only knew about the first.

```
A) project → opportunity (draft) → launch → submissions → (auto)approve
B) quote → quote launch (creates AND launches in one call)
```

### 1.2 Every endpoint we will touch

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/projects` | create project |
| `POST` | `/opportunities` | create **draft** opportunity |
| `GET` | `/opportunities/{opportunityId}` | read status + `screening_stats` |
| `POST` | `/opportunities/{opportunityId}/launch` | activate |
| `POST` | `/opportunities/{opportunityId}/stop` | end recruitment |
| `GET` | `/opportunities/{opportunityId}/submissions` | poll for labels |
| `GET` | `/submissions/{submissionId}` | per-submission detail |
| `POST` | `/submissions/{submissionId}/approve` | billing event |
| `GET` | `/filters` | discover slugs + **operators** at runtime |
| `GET` | `/organizations/current/context` | **`balanceDollars`** — live credit budget |
| `POST` | `/quotes` | instant price estimate |
| `POST` | `/hooks/subscriptions` | create webhook subscription |
| `POST` | `/hooks/subscriptions/{id}` | **confirm** subscription (required) |
| `GET` | `/hooks/event-types` | enumerate event types |

Full endpoint inventory (47 operations) is in the spec; the above is what Overwatch calls.

### 1.3 `POST /opportunities` — exact request shape

Read verbatim from the spec. **`required`: `title`, `project_id`, `num_participants`,
`business_type`, `tasks`.**

| Field | Type | Constraint |
|---|---|---|
| `title` | string | 1–200 |
| `internal_title` | string | ≤200 |
| `description` | string | ≤8000 |
| `project_id` | string | required |
| `num_participants` | integer | **1–1000** |
| `business_type` | enum | `b2c` \| `b2b` |
| `tasks[]` | array | minItems 1 |
| `tasks[].sequence` | integer | ≥1, required |
| `tasks[].task_type` | enum | **`interview` \| `file_upload` \| `activity`** — required |
| `tasks[].review_type` | enum | `auto_approve` \| `manual_review` \| `self_report` — required |
| `tasks[].task_url` | string(uri) | optional |
| `tasks[].title`, `.description` | string | optional |
| `tasks[].duration_minutes` | integer | ≥1 |
| `filters[]` | array of single-key objects | see §1.6 |
| `unrestricted_audience` | boolean | |
| `screening_questions[]` | array | see §1.7 |
| `quotas[]` | array | legacy per-question form |
| `cross_quotas[]` | array | maxItems 200, see §1.8 |
| `device_types[]` | enum[] | `desktop`, `mobile_ios`, `mobile_android`, `tablet_ios`, `tablet_android`, `other` |
| `expected_days_to_complete` | integer | **minimum 5**, maximum 50000000 |
| `feasibility_request_id` | string | |

**Opportunity `status` enum:** `draft`, `active`, `fulfilled`, `paused`, `stopped`, `completed`.

### 1.4 Task read-back differs from the write shape

The response `tasks[]` carries fields the request does not accept — read-only, server-derived:

```
sequence, task_type, review_type, task_url (nullable), participant_url_template,
title, description, duration_minutes, available_after_sequence,
available_after_delay_minutes, provider, calendar_owner, event_type_url, instructions
```

`participant_url_template` is the field that matters to us: **you send `task_url`, and Terac reads
back `participant_url_template`.** I infer (not documented) that this is where the
`{{participant_id}}` substitution is recorded. We will confirm by reading the create response in
`scripts/probe_terac.py` before Round 1 launches.

### 1.5 Submissions — the join key exists

`GET /opportunities/{opportunityId}/submissions`
query params: `limit` (1–100, default **25**), `cursor`, `status`.

`status` enum — **7 values, and `approved` is one of them**, so `?status=approved` polling per
`SPECS.md` §5.4 is valid:

```
screen_passed, screened_out, in_progress, awaiting_review, approved, rejected, abandoned
```

Response:

```
data[]:
  id, opportunity_id, status, participant_id, created_at, updated_at
  screening_outcome: passed | failed | review | null
  screening_answers[]: { key, question, answer[], outcome: qualify|reject|not_important|review|null }
pagination: { next_cursor (nullable), has_more }
dashboard_url (nullable)
```

**`participant_id` is present on every submission.** This is the entire basis of the experiment: our
task page captures `{{participant_id}}` from the URL, and this field lets us join a Terac submission
to the labels that participant gave us. Verified in the spec, not assumed.

`GET /submissions/{submissionId}` adds `tasks[]: { sequence, task_type, status }`.

### 1.6 Filters

Format `{type}--{attribute}`, one single-key object per filter
(<https://terac.com/docs/developers/guides/filters>).

Operators: `$eq`, `$in`, `$gte`, `$lte`, `$all`, `$nin` — with documented applicability per type.
In the spec the filter value is `additionalProperties` typed `number | string | string[]`, i.e. the
API does **not** validate operator/slug pairs at the schema level — it validates them server-side and
returns `BAD_REQUEST` with `details[].field` (see §2).

Catalog (<https://terac.com/docs/developers/guides/filters/catalog>) — the slugs we use:

- `multi_select--country`, `multi_select--state`, `multi_select--city` (hierarchical composite IDs:
  `US-CA`, `US-CA-Los Angeles`)
- `integer--age` (18–99)
- `multi_select--language`
- `reference--has_taken_study`, `reference--has_not_taken_study` — "Has NOT completed specific
  study(s)"

`GET /filters` returns per-slug `operators[]`, plus `min`/`max` on integer types and `options_url` on
select types. **This is how we resolve the round-2 filter format at runtime instead of guessing** —
see §9 UNKNOWN 5.

### 1.7 Screening questions

<https://terac.com/docs/developers/guides/screening-questions>

`pick`: `one` | `any` | `boolean` | `text` | `grid`.
`qualify_logic`: `may` | `must` | `must_one_of` | `reject` | `review`.
`answers` minItems **2**, required for choice questions, omitted for `text`/`grid`.

Also available and not in `SPECS.md`: `question_rich_text` (GFM markdown display copy),
`allow_free_text`, `min_qualifying`, `display_condition`, `conditional_rules` (consistency /
anti-falsification checks with `outcome: reject|review`), `skip_rules` (branching), `allow_paste`
(open-ended paste blocking — on by default, i.e. pasting is blocked).

`key` is client-defined and optional, **but** it reads back on every question and is returned as
`screening_answers[].key` on the submission. That is the documented way to join an answer to its
question. Required when referenced by a quota.

`GET /opportunities/{id}` returns `screening_stats` per question: `graded`, `rejected`,
`rejection_rate`, `answers[].{text,selected,share}`. Useful for the dashboard: it tells us live
whether our screener is too strict.

### 1.8 Quotas

<https://terac.com/docs/developers/guides/quotas>. `cross_quotas[]`, required
`label`, `conditions`, `target` on write (`join`/`quota_type` default). `conditions` 1–26.
`quota_type`: `minimum` (default) | `maximum` | `exact`. Cells sharing a `dimension` token are summed
as one interlocked cross-tab.

Recruitment stops when **both** every `minimum`/`exact` cell is met **and** `num_participants` is
reached. `maximum` cells never extend recruitment.

### 1.9 Quotes and feasibility — instant pricing exists

`POST /quotes` → `{ quoteId, totalCost, costPerParticipant, timelineHours, submissionCount,
expiresAt }`, all required in the response. Request requires `taskDescription`, `panelDescription`,
`timelineHours` (**minimum 72**, max 720), `submissionCount` (1–999).

This is **synchronous pricing** — it partially answers UNKNOWN 6. `POST /feasibility/requests` is the
separate, human-priced path: its list endpoint documents `costPerParticipant` as
"null until the request has been priced", and statuses `RECEIVED / RESPONDED / WON / LOST`, which is
an out-of-band human workflow. **Conclusion: use `/quotes` for a price, never `/feasibility` — it
will not return in time.**

`POST /quotes/{quoteId}/launch` takes `{ name, projectId }` and per its description "Creates and
launches a research opportunity from a previously created quote. AI generation, billing, and
activation happen asynchronously after this returns." **We do not use this path** — it AI-generates
the opportunity, so we would lose control of the screening questions and task URL, which is the one
thing we cannot lose.

### 1.10 Webhooks

<https://terac.com/docs/developers/guides/webhooks>

Two calls to set up, and **the second one is mandatory** — `SPECS.md` omitted it:

1. `POST /hooks/subscriptions` `{target_url, event_types[]}` → returns `secret` (`whsec_…`),
   `confirmed_at: null`.
2. `POST /hooks/subscriptions/{id}` with `-d '{}'` → Terac POSTs a signed `webhook.ping`. Answer
   `2xx` and it activates. **Anything else returns `412` and nothing is confirmed.** A subscription
   receives nothing until confirmed.

Secret is recoverable via `GET /hooks/subscriptions/{id}/secret` — losing it does not force rotation.

Headers on every delivery:

| Header | Meaning |
|---|---|
| `X-Terac-Request-Signature` | `base64(HMAC-SHA256(secret, timestamp + raw body))` |
| `X-Terac-Request-Timestamp` | Unix seconds, part of the signed string, changes per retry |
| `X-Event-ID` | unique per delivery, **stable across retries** → dedup on this |
| `X-Timestamp` | ISO-8601 event time, stable across retries → order by this |

The reference implementation rejects `|now - timestamp| > 300`s and uses a constant-time compare.
`CLAUDE.md`'s description of the signature was **correct**.

Event types are only two today (`GET /hooks/event-types` enum): `submission.status.change`,
`submission.approved`. Payload:

```json
{ "event_type": "...", "event_id": "dlv_7h3k9", "resource_id": "sub_abc123",
  "occurred_at": "...", "opportunity_id": "...", "from": "screening", "to": "screened_out" }
```

Retries: 12 after the first attempt, 1 min → exponential to 12 h, ~2.5 days total. `5xx`/`408`/`429`
retried; other `4xx` treated as deliberate rejection and **not** retried. Redirects never followed.
10 s timeout. 100% failure for 5 consecutive days auto-disables.

### 1.11 Organization context

`GET /organizations/current/context` → `markdown`, `organizationId`, `organizationName`,
`organizationSlug`, **`balanceDollars`**, `dashboard.{home,opportunities,drafts,feasibility,finance,api_keys}`.

`balanceDollars` resolves UNKNOWN 3 (credit budget) **programmatically at runtime**, so
`num_participants` can be computed from real balance instead of asked at a booth. This is wired into
the dashboard.

---

## 2. Terac gotchas — every constraint that produces a 4xx

Error envelope (<https://terac.com/docs/developers/guides/errors>):

```json
{ "error": { "code": "BAD_REQUEST", "message": "...",
             "details": [ { "field": "filters[0]", "message": "..." } ] } }
```

Codes: 400 `BAD_REQUEST`, 401 `UNAUTHORIZED`, 404 `NOT_FOUND`, 409 `CONFLICT`, 429 `RATE_LIMITED`,
500 `INTERNAL_SERVER_ERROR`. The spec additionally references `error.FORBIDDEN` (403) on `/quotes`,
which the errors guide does not list — minor doc gap.

| # | Constraint | Failure | Source |
|---|---|---|---|
| 1 | **`task_type` has no `survey` value** — only `interview`/`file_upload`/`activity` | `BAD_REQUEST` | spec enum |
| 2 | Every `POST` needs a JSON body + `Content-Type`, even path-only actions | `415` | webhooks guide |
| 3 | `expected_days_to_complete` minimum **5** | `BAD_REQUEST` | spec `minimum: 5` |
| 4 | `cross_quotas` without `screening_questions` | `BAD_REQUEST` | quotas guide |
| 5 | `answers` needs **minItems 2** | `BAD_REQUEST` | spec |
| 6 | `num_participants` max **1000** | `BAD_REQUEST` | spec |
| 7 | Launching an already-active opportunity | `409 CONFLICT` | errors guide |
| 8 | Unrecognized filter slug | `BAD_REQUEST` | filters guide |
| 9 | `display_condition` referencing a *later* question | `BAD_REQUEST` | screening guide |
| 10 | Webhook `target_url` non-https / private / loopback IP | refused | webhooks guide |
| 11 | Duplicate `target_url` per organization | `409` naming the existing sub | webhooks guide |
| 12 | Unconfirmed subscription receives **nothing** (silent, not an error) | — | webhooks guide |
| 13 | Signing re-serialized JSON instead of raw bytes | signature mismatch | webhooks guide |
| 14 | Subscribing to both event types → approvals arrive **twice**, different `X-Event-ID` | — | webhooks guide |
| 15 | `PATCH event_types` **replaces**, does not append | silent loss | webhooks guide |
| 16 | Changing `target_url` clears confirmation | silent stop | webhooks guide |
| 17 | Secret rotation has **no overlap window** | signature failures | webhooks guide |
| 18 | `state`/`city` filter options return **empty** without `country_id` | silent empty | catalog |
| 19 | `pick: "text"` forces **manual review**, cannot be quota'd | breaks auto_approve | screening guide |
| 20 | `/quotes` `timelineHours` **minimum 72** | `BAD_REQUEST` | spec |

---

## 3. Band wiring

Verified against <https://docs.band.ai/integrations/sdks/reference.md>,
<https://docs.band.ai/integrations/sdks/tutorials/langgraph.md>,
<https://docs.band.ai/core-concepts/chat-rooms.md>, <https://www.band.ai/hacker-guide>.

Install `band-sdk[langgraph]`, import `band`. PyPI confirms `band-sdk` **1.6.0**.

```python
from band import Agent, AdapterFeatures, Emit
from band.adapters import LangGraphAdapter
from band.config import load_agent_config

adapter = LangGraphAdapter(
    llm=ChatOpenAI(model=...),
    checkpointer=InMemorySaver(),
    custom_section=PREAMBLE + ROLE_PROMPT,
    features=AdapterFeatures(emit={Emit.EXECUTION}),
)
agent_id, api_key = load_agent_config("scout")   # reads ./agent_config.yaml
agent = Agent.create(adapter=adapter, agent_id=agent_id, api_key=api_key)
await agent.run()
```

`Agent.create()` signature (reference): `adapter, agent_id, api_key,
ws_url="wss://app.band.ai/api/v1/socket/websocket", rest_url="https://app.band.ai", config,
session_config, contact_config, preprocessor`.

`LangGraphAdapter` signature: `llm, checkpointer, graph_factory, graph, prompt_template="default",
custom_section="", additional_tools=None, features=None, history_converter=None,
recursion_limit=50`.

`AdapterFeatures` values: `Emit.EXECUTION` (tool_call + tool_result into the room timeline),
`Emit.TASK_EVENTS`, `Emit.THOUGHTS`, `Capability.MEMORY` (enterprise only), `Capability.CONTACTS`.

**Platform tools, always on** (no `features=` needed — hacker guide is explicit):
`band_send_message(content, mentions=None)`,
`band_send_event(content, message_type, metadata=None)` where `message_type` ∈
`thought | error | task | tool_call | tool_result`,
`band_add_participant(name, role="member")` — **by name, not UUID**,
`band_remove_participant(name)`, `band_get_participants()`,
`band_lookup_peers(page=1, page_size=50)`, `band_create_chatroom(task_id=None)`.

Contact tools require `Capability.CONTACTS`, and we do not need them: all five agents sit under one
owner, so they are same-registry peers, visible to `band_lookup_peers` with `source: "registry"` and
zero setup.

Confirmed behaviours that shape the design:

- Agents receive **only** messages that @mention them. Humans in the room see everything, including
  every event. This is why the Band console is our judging evidence.
- Agents never receive their own messages, and **never receive events at all** — `tool_call`,
  `tool_result`, `thought`, `error`, `task` go to user clients as `event_created` only. Events are
  for the audit trail and for humans, not for agent-to-agent signalling. Any actual handoff must be
  a `band_send_message` with an @mention.
- **One live WebSocket per `agent_id`; the newest connection wins and the older is dropped with no
  error on either side.** Free tier allows 10 registered agents — enough for our five plus AuthProbe.
- Free tier covers the entire Agent API, the WebSocket, and every `band_*` tool.

**Delete test** (hacker guide): four signals — dependent handoff, runtime roster, a boundary Band
enforces, a blockable verdict. `AGENTS.md`'s claim to hit three of four is accurate: we have
dependent handoff (Recruiter's opportunity is built from Triage's selection), runtime roster
(AuthProbe recruited per case), and blockable verdict (Critic vetoes Bursar's release).

---

## 4. Render Workflows

Package name confirmed on PyPI: `render_sdk` **0.7.0**, "Python SDK for Render Workflows"
(`requirements.txt` pins `>=0.6.0`, satisfied). Detailed doc research was delegated; findings and
any gaps are recorded in §9. The orchestration layer is **fourth in the cut order** in
`docs/RUNBOOK.md`, so nothing on the critical path depends on it: `app/pipeline.py` runs the same
scan inline via `asyncio`, and `workflows/scan_workflow.py` is a thin durable wrapper over it.

---

## 5. Superserve

Package name confirmed on PyPI: `superserve` **0.8.2**, "Python SDK for the Superserve sandbox API".
Detailed API research delegated; see §9 for what remains unverified. Superserve is **third in the cut
order**. `app/sources/playwright_source.py` therefore runs Playwright locally by default and treats
the sandbox as an optional isolation layer selected by env var, so an unavailable sandbox API cannot
block the 12:30 spine gate.

---

## 6. Whop

Package name confirmed on PyPI: `whop_sdk` **0.0.41**, "The official Python library for the Whop
API". Detailed doc research delegated; see §9. Revenue is **second in the cut order**. The Whop
client is written against `httpx` with the checkout URL and metadata join key behind named
constants, so correcting them is a one-line edit.

---

## 7. Replay QA

`docs/DECISIONS.md` 002 sets an **11:00 decision gate**: if there is no programmatic access by then,
switch to `PlaywrightSource` and never revisit. Detailed research delegated; see §9.

Regardless of the answer, the `BugSource` protocol in `app/sources/base.py` means the choice costs
one class. `PlaywrightSource` is implemented and is the default, so the gate can be failed safely.

---

## 8. CONTRADICTIONS

**8.1 `qualify_logic: "must"` on `pick: "any"`.**
`CLAUDE.md` says: *"`qualify_logic: "must"` is invalid on `pick: "any"` — use `must_one_of`."*
The docs say the opposite: *"`must` — Participant must select this exact answer **(valid with `one`
or `any`)**"* and *"On multi-select (`pick: "any"`), `must` means this exact answer is required,
while `must_one_of` means at least one of a group."*
(<https://terac.com/docs/developers/guides/screening-questions>)
`scripts/probe_terac.py`'s original comment — *"`must` is only valid on `pick:"one"`"* — is also
wrong. **Not resolved by preference: the docs win, but the real constraint is the inverse of what our
notes claimed, and single-select collapse means `may`/`must`/`must_one_of` are indistinguishable on
`pick: "one"` and all read back as `must`.**

**8.2 Band platform tool names.**
<https://docs.band.ai/core-concepts/chat-rooms.md> names the participant tools
`list_available_participants_service`, `add_participant_service`, `remove_participant_service`.
<https://docs.band.ai/integrations/sdks/reference.md> and the hacker guide name them
`band_lookup_peers`, `band_add_participant`, `band_remove_participant`. Both are current pages. I use
the `band_*` names because the SDK reference is the Python surface we actually call, but I am not
resolving which is "correct" — the core-concepts page may be describing internal service names.

**8.3 `AnthropicAdapter` prompt kwarg.**
The hacker guide passes `AnthropicAdapter(model="claude-opus-5", prompt="...")`. The SDK reference
lists no `prompt` parameter — it has `system_prompt` and `custom_section`. One of the two is stale.
Does not affect us (we use `LangGraphAdapter`), but it is a caution against trusting the guide's
snippets over the reference.

**8.4 Terac recruitment latency — the contradiction that sets the schedule.**
The API enforces `expected_days_to_complete` **minimum 5** (spec) and `/quotes` enforces
`timelineHours` **minimum 72**. Both encode a multi-day floor. The event framing promises results
within hours. These cannot both describe the same system. **Unresolved, and it is the riskiest thing
in this document** — see §9 UNKNOWN 2. `scripts/probe_terac.py` exists solely to measure the real
number.

**8.5 API reference discoverability.**
The guides index links to an "API Reference — Full endpoint reference generated from the OpenAPI
specification", but `/docs/developers/api-reference` returns **404**. The spec is nonetheless served
at `/api/external/v2/openapi.json`. Documentation bug, not an API bug.

---

## 9. STILL UNKNOWN

Cross-referenced with `docs/SPECS.md` §10. Each is implemented behind a named constant carrying an
`# UNKNOWN:` comment, so correcting it is one edit.

| # | Question | Status after this pass | Where it lives in code |
|---|---|---|---|
| 1 | 5-day `expected_days_to_complete` minimum waived for hackathon keys? | **Still unknown.** Confirmed the API enforces `minimum: 5`; whether hackathon keys bypass it is not documented. | `EXPECTED_DAYS` in `app/clients/terac.py` |
| 2 | Realistic minutes-to-first-completion? | **Still unknown and unknowable from docs** — see §8.4. Only measurable. | measured by `scripts/probe_terac.py` |
| 3 | Credit budget per team? | **Resolved programmatically.** `GET /organizations/current/context` → `balanceDollars`. | `TeracClient.org_context()` |
| 4 | REST + webhooks, or MCP + polling? | **Resolved: REST + webhooks, and both work.** 47 REST operations, a documented webhook subscription lifecycle. A `terac.com/mcp` page exists in the sitemap but I did not fetch it, so I cannot say what the MCP surface offers. | `app/clients/terac.py` + `/hooks/terac` |
| 5 | Exact value format for `reference--has_not_taken_study` | **Still unknown.** The slug is confirmed to exist and to mean "Has NOT completed specific study(s)". Its `operators[]` are **not** in the catalog page and the spec types filter values loosely as `number \| string \| string[]`. **Mitigation:** `GET /filters` returns `operators` per slug at runtime, so `scripts/probe_terac.py --filters` prints the real operator list rather than guessing. | `ROUND2_EXCLUSION_OPERATOR` in `app/clients/terac.py` |
| 6 | Feasibility pricing instant or out of band? | **Effectively resolved.** `/quotes` is synchronous and returns `totalCost` + `costPerParticipant`. `/feasibility/requests` is the human-priced path (`costPerParticipant` "null until priced", `RECEIVED/RESPONDED/WON/LOST`). Use quotes. | `TeracClient.quote()` |
| 7 | "Best Use of Terac" criteria | **Unknowable before 09:15.** No doc exists yet. | n/a |
| 8 | Replay QA programmatic access | **RESOLVED: yes.** Live REST API, 19 operations, spec fetched (§12.3). The 11:00 gate can be passed. | `app/sources/replay.py` |
| 9 | Render Workflows on hackathon credits | Delegated research. Not on the critical path. | `workflows/scan_workflow.py` |
| 10 | Whop payout verification same-day | **Not answerable from docs** — it is an account/KYC question. | n/a |

**New unknown discovered in this pass (not in `SPECS.md` §10):**

| # | Question | Why it matters |
|---|---|---|
| 11 | Which `task_type` does a hosted web survey use — `activity`, or is `interview` expected for anything with a `task_url`? | `survey` does not exist. If `activity` is wrong, **both rounds fail to create.** Highest-value single question to ask at the booth. Implemented as `TASK_TYPE` constant. |
| 12 | Does `task_url` accept `{{participant_id}}` templating, and is that what `participant_url_template` reads back? | Without participant identity in the URL, the dataset cannot be joined and the experiment is void. Verify by reading the create response. |

---

## 10. Corrections to `CLAUDE.md` and `SPECS.md`

Blunt, as requested.

### 10.1 `task_type: "survey"` does not exist — this would have broken both rounds

`SPECS.md` §5.2 and the original `scripts/probe_terac.py` both send `"task_type": "survey"`. The spec
enum is `["interview", "file_upload", "activity"]`. **Every opportunity creation in the plan as
written returns `400 BAD_REQUEST`.** This is exactly the failure `CLAUDE.md`'s anti-hallucination
protocol was written to prevent, and it was sitting in the file that was going to be run first at
10:45. Corrected to `activity` (marked as an inference, UNKNOWN 11).

### 10.2 The `must` / `must_one_of` gotcha is stated backwards

See §8.1. `CLAUDE.md`'s "Known API gotchas" says `must` is invalid on `pick: "any"`. The docs say it
is valid on both, and that `must` vs `must_one_of` is a *semantic* distinction (this exact answer vs
any of a group), not a validity constraint. Following our note would have produced screeners that
qualify the wrong people — a silent data-quality bug, worse than a 400.

### 10.3 Webhook subscriptions must be **confirmed**, and `SPECS.md` never mentions it

`SPECS.md` §5.4 describes a receiver at `/hooks/terac` with HMAC and dedup, but not the two-step
create-then-confirm handshake. An unconfirmed subscription **silently receives nothing**. There is no
error to debug — the room just stays quiet, which at 16:00 is the worst possible failure mode.
Decision 007's polling loop is what would have saved us; now we do both properly.

### 10.4 `CLAUDE.md` says "no SDK exists" for Terac — true, and it does not matter

Correct that there is no Terac SDK, but the OpenAPI spec is public, which is strictly better: the
client is generated against a machine-readable contract rather than prose. `CLAUDE.md`'s
"hand-rolled client" framing understates what is available. A copy of the spec is vendored at
`docs/terac_openapi.json` so the contract is greppable offline.

### 10.5 Argument against the pluggable `BugSource` abstraction — I disagree with the objection

`KICKOFF.md` invites me to argue against `SPECS.md`'s decisions. `BugSource` is the one abstraction I
would keep. It is two methods, it has two real implementations, and Decision 002 hangs an 11:00
go/no-go on swapping them. That is not speculative flexibility; it is a decision gate encoded in a
type. Keep it.

### 10.6 Argument against `precision@10` as primary metric — this one is weak

With 20 findings reviewed at 3 raters each, `precision@10` moves in increments of **0.1**. A one-
finding change in the top ten looks like a ten-point swing, and the metric has no confidence interval
attached in the plan. At this sample size it is closer to anecdote than measurement.

I am **not** changing it, because it is the headline number in `SPECS.md` §2 and switching metrics
mid-build is worse than a coarse metric. But `app/metrics.py` also computes **mean average precision**
and reports the **Wilson interval on the confirmation rate** alongside it, so the write-up can be
honest about resolution instead of implying 0.55→0.65 is a precise result. Per `RUNBOOK.md`'s "if the
result is negative" section, reporting resolution honestly is the higher-scoring move.

### 10.7 The batching scheme is sound but under-specified on rater assignment

Decision 004 (12 participants × 5 findings = 60 judgments over 20 findings at 3 raters) only works if
assignment is *deterministic and balanced*. If each participant gets a random 5 of 20, the expected
raters per finding is 3 but the variance leaves some findings with 1 rater and some with 6 — and
"majority of 3" is then undefined for part of the set. `app/triage.py` assigns via a fixed
round-robin over a deterministic shuffle seeded per scan, which guarantees exactly 3 raters per
finding, and randomizes *order within* each participant's set to address the fatigue risk the
decision itself flags.

### 10.8 `SPECS.md` §5.2's example payload has a second, quieter bug

It sets `cross_quotas` targets of 6 (Desktop) + 4 (Mobile) = 10 against `num_participants: 12`. That
is legal — the quotas guide says cell targets need not sum to `num_participants` — but combined with
`quota_type: "minimum"` on both it means recruitment continues until *both* floors are met *and* 12
are reached, so the effective floor is 10 of 12 constrained. With a 3-minute general-population task
and unknown fill latency, the tighter the interlock the slower the fill. Round 1 launches with the
device quota as a **`minimum` on desktop only**, leaving mobile unconstrained, so a slow mobile cell
cannot stall the round we cannot afford to be late.

---

## 11. Corrections found by building and running the thing (post-Phase-0)

Everything above came from reading documentation. Everything below came from installing the
packages, importing them, and running the pipeline end to end. Each item is a place where the
docs were wrong or silent in a way that would have cost time on the day.

### 11.1 `python-multipart` was missing, and every task-page submission returned 500

Not in `requirements.txt`. Starlette needs it to parse a form body, and without it
`await request.form()` raises `AssertionError` — so `POST /t/r1/{scan_id}` answered **500 on
every submission**. The task page rendered perfectly; only submit failed.

This is the worst-shaped bug available to this project. Nothing crashes at startup, the page
looks right on a phone, and the failure is invisible until a participant presses the button —
by which point we have paid for judgments that were never stored. Found by simulating twelve
participants against a live server, which is the only thing that would have found it.

Added to `requirements.txt` with that reasoning in a comment.

### 11.2 `pip install -r requirements.txt` could not resolve at all

The original file pinned `pydantic==2.9.*`, `httpx==0.27.*` and `python-dotenv==1.0.*` while
leaving `langgraph`, `langchain-openai`, `anthropic`, `band-sdk`, `superserve` and `whop_sdk`
unpinned. pip backtracked for **29 minutes** and died with `ResolutionTooDeep: 200000`.

Three pins were impossible rather than merely slow:

| Pin in the doc | Required by | Actual constraint |
|---|---|---|
| `python-dotenv==1.0.*` | band-sdk 1.6.0 | `>=1.2.2` |
| `httpx==0.27.*` | render-sdk 0.7.0 | `>=0.28.1,<0.29` |
| `pydantic==2.9.*` | langchain-core 1.5.x, band-sdk | `>2.9` |

`requirements.txt` now pins every package to a version that was installed and imported
successfully. Resolution takes 58 seconds.

### 11.3 Band SDK surface, verified by introspection

`docs/AGENTS.md`'s wiring snippet is accurate — `from band.adapters import LangGraphAdapter`
resolves, `AdapterFeatures(emit={Emit.EXECUTION})` is correct, and
`load_agent_config(key)` returns `(agent_id, api_key)`. Confirmed against band-sdk 1.6.0:

```
band.Agent.create(adapter, agent_id, api_key, ws_url=…, rest_url=…, config=…)
band.Emit -> EXECUTION | THOUGHTS | TASK_EVENTS | USAGE
band.adapters.LangGraphAdapter(llm=…, checkpointer=…, custom_section=…,
                               additional_tools=…, features=…)
band.config.load_agent_config(agent_key, *, config_path=None) -> tuple[str, str]
```

One thing the snippet omits: it passes our domain tools nowhere. **`additional_tools=` is the
parameter that attaches them**, and an adapter built without it produces an agent that can
discuss scanning and cannot scan. `app/agents/runtime.py` passes it.

### 11.4 Render Workflows composition model, verified

`render_sdk` 0.7.0 exposes `task`, `start`, `Options`, `Retry`. The composition model is not in
our docs and is worth writing down: `TaskCallable`'s own docstring is *"A callable that can be
awaited to run as a subtask"*, so inside a task, `await other_task(...)` runs it as a durable
subtask. `render_sdk.start()` reads `RENDER_SDK_MODE` and `RENDER_SDK_SOCKET_PATH` to decide
whether to register tasks or run them.

### 11.5 The SSRF guard turned a malformed URL into a 500

`assert_safe_target("javascript:alert(1)")` raised `ValueError` instead of
`UnsafeTargetError`. The input has no `://`, so the guard prefixed `https://`, producing
`https://javascript:alert(1)` — whose "port" is `alert(1)`, and `urlsplit().port` raises on
that. `/api/scan` maps `UnsafeTargetError` to 400 and everything else to 500, so a customer
pasting a typo got "internal server error".

Fixed two ways: a scheme with no authority is now rejected before parsing, and `.hostname` /
`.port` access is wrapped so no malformed input can produce a 500. Covered by
`tests/test_security.py::test_malformed_input_is_a_refusal_not_a_crash`.

### 11.6 `/` served the React build with no assets

Vite is configured with `base: "./"` so `dist/` works both at `/` and mounted at `/app`. But
`/` returns `dist/index.html` directly, where the relative asset URLs resolve to `/assets/*` —
which was not mounted. The landing page loaded as unstyled HTML with no JavaScript. `app/main.py`
now mounts `dist/assets` at `/assets`.

### 11.7 Round 2 had no reviewable prepare step

`prepare_round1` exists so the task pages are servable before any credit is spent. Round 2 had
no equivalent: assignment creation was inline in `launch_round2`, so `/t/r2/{scan_id}` could not
be opened on a phone until the opportunity was already live and paid for. Extracted as
`pipeline.prepare_round2`, which `launch_round2` now calls.

### 11.8 The rehearsal, and what it does and does not show

`scripts/rehearse_experiment.py` runs the whole loop against simulated raters. On the seed
fixture it produced precision@10 0.90 → 1.00 and MAP 0.947 → 1.00, demoting the
"ReferenceError: analytics is not defined" finding that the v1 baseline ranked sixth.

> **Superseded — see §12.5.** Those figures are *in-sample*: v2 was scored against the same
> labels that built it. The honest held-out figure on this fixture is a **tie**, 1.000 vs 1.000.
> The `+0.10` was largely an artifact of the measurement, not an effect of the recalibration.

**That is not a result and must never be presented as one.** The judgments are scripted. What it
establishes is narrower: a scan, 60 form submissions, the recalibration and the metrics are
genuinely wired to each other, and `precision@10` *can* move. If v1 and v2 had tied here they
would tie on real labels too, and it is much better to learn that before twelve people are paid.

The real numbers come from Terac and are whatever they are.

---

## 12. Second verification pass — the three sponsor APIs I had taken on trust

Phase 0 delegated Whop, Replay and Render to subagents and recorded their answers in §9 as
"delegated". This pass read the primary sources. Every item below is a correction to code that was
already written and passing tests, which is the argument for the pass.

### 12.1 Whop: the base URL was wrong, and the signature scheme was guessed

`WHOP_API_BASE` was `https://api.whop.com/api/v5`. The installed SDK hardcodes
`https://api.whop.com/api/v1` (`whop_sdk/_client.py:256`). **Every checkout call would have
404'd.** Nothing caught it because no live call had been made.

The request and response shapes are now read from the Stainless types generated from Whop's
OpenAPI spec, not from notes:

- `plan_id` and `plan` are **mutually exclusive** ("Mutually exclusive with `plan`"). Sending
  both is a 400. `WHOP_PLAN_ID` therefore selects which one we send, and the inline path is what
  runs when no plan was pre-created — the likely hackathon state.
- `metadata` is top-level and documented as "Custom key-value metadata copied to payments and
  memberships", which is the passthrough our join key depends on.
- The create response returns `purchase_url`, "Checkout URL you can send to customers". We now
  prefer it over reconstructing `https://whop.com/checkout/{plan_id}`, which is kept only as a
  fallback.
- The response `id` is the `ch_…` checkout configuration id, and the payment carries
  `checkout_configuration_id`. That is a **second join key**, now stored as `Order.checkout_id`
  and used when `metadata.order_id` is absent.

`verify_whop_signature` previously compared a bare hex HMAC of the body, marked `# UNKNOWN`. Whop
implements **Standard Webhooks**: `base64(HMAC-SHA256(secret, "{webhook-id}.{webhook-timestamp}.
{raw body}"))`, header `webhook-signature: v1,<sig>`, five-minute tolerance, and the header may
carry several space-delimited signatures during secret rotation. The old implementation would
have rejected every genuine webhook.

**This is not Terac's scheme** — Terac signs `timestamp + body`, no id, no dots. The two are close
enough to look interchangeable, so they are separate functions and
`test_the_two_providers_are_not_interchangeable` pins that.

One more correction: `_process_whop_event` branched on `"succeed" in action` reading `body["action"]`.
The envelope field is `type`. Whop's own docs disagree about `status` (`"paid"` with
`"substatus": "succeeded"` in the spec example, `"succeeded"` in the guide), so branching on
`type == "payment.succeeded"` is the only unambiguous test.

### 12.2 Render: we were using the deprecated API

`render_sdk/__init__.py` says, in the source: `# Deprecated: use Workflows.task and
Workflows.start() instead`. `workflows/scan_workflow.py` used module-level `render_sdk.task` /
`render_sdk.start`. Migrated to `app = Workflows()`.

Not a cosmetic change: the two signatures differ. Module-level takes `options=Options(retry=…,
timeout_seconds=…)`, the instance method takes `retry=` and `timeout_seconds=` directly. Mixing
them silently drops configuration.

Also confirmed: **there is no durable sleep and no wait-for-external-event primitive.** This
retroactively justifies the shape `scan_and_verify` already had — it ends after launching round 1
rather than awaiting submissions, because there is no way to await them. Round 2 is a separate
entry point driven by the webhook.

### 12.3 Replay QA: UNKNOWN 8 resolved — the API is real

Fetched `https://qa.replay.io/api/v1/openapi.json` — HTTP 200, "Replay QA API 1.0.0", 19
operations, `bearerAuth` described as "API token from Settings > API Token (starts with lqa_)".
`app/sources/replay.py` was a deliberate `NotImplementedError` and is now a real client.

Verified from the spec: `POST /api/v1/projects` with `required: [name, target_url]`, plus optional
`budget` ("Defaults to 20 when omitted", "~10 = smoke test, 20-50 = thorough"),
`enabled_polish_passes`, `instructions`, `webhook_url`, `use_reverse_proxy`.

**The limit of that verification, stated plainly: the spec documents no response schema for any
endpoint** — `GET /projects/{id}/bugs` carries only `description: "List of bugs"`. So the bug field
names come from the `webhook_url` field's documented payload: `{ body, referrer, callback_url,
bug_id, title, severity, description, reproduction_steps, expected_behavior, actual_behavior,
replay_recording_id, analysis, polish_category }`. Whether the *list* endpoint uses those same
names is an inference. `_to_finding` degrades field-by-field instead of raising, and
`scripts/probe_replay.py` diffs a real bug against that list and names anything missing.

Two smaller consequences:

- We poll instead of taking `webhook_url`. A webhook needs a public URL that the 11:00 gate
  cannot depend on. `_await_bugs` stops when the count has been flat for three polls, and on
  timeout returns what it has — a partial scan is usable, an exception is a dead gate.
- Replay reports no confidence score, so `agent_confidence` is a flat 0.6. That is the honest
  position for a finding whose confidence is unknown, and round 1 is what replaces the guess.

The `polish_category` values (`layout-shift`, `accessibility`, `glitches`, `user-experience`,
`ui-details`, `network-performance`, …) map directly onto the category-level recalibration in
SPECS.md §2 — "drop finding categories with <30% confirmation" — which is a better fit than the
categories the Playwright source invents. `seo` and `react-rendering` are deliberately disabled:
they produce findings only a developer can adjudicate, and a Terac panel judging them from a
screenshot would add noise to round 1.

### 12.4 `make probe` was broken for two of four services

Running a file inside `scripts/` puts `scripts/` on `sys.path`, not the repo root, so
`from app.config import settings` raised `ModuleNotFoundError`. `probe_whop` and `probe_band` both
failed instantly; `probe_terac` survived only because it imports nothing from `app`. Fixed once in
the `Makefile` with `PYTHONPATH=.` rather than a `sys.path` hack in each script.

A documented command that fails on import is worse than a missing one, and this is exactly the
class of bug the probe scripts exist to catch — the probes themselves just were not run.

---

## 12.5 The headline metric was circular. This is the most serious defect found.

Found during the full-code review, not by a failing test. Nothing crashed; every test passed
before and after. The measurement was simply not measuring what it claimed.

### What was wrong

`triage.rank_v2` is *fitted* to the round-1 labels: it multiplies rejected findings by 0.15 and
promotes confirmed ones. `pipeline.results` then handed those same labels to `metrics.evaluate`,
which scored both rankings against them. **v2 was scored on the labels that built it** — training
on the test set.

The old `precision_at_k` compounded it by truncating to `k` *before* filtering to labeled
findings. Because v2 deliberately hoists labeled-confirmed findings into its top 10, v2's
denominator collapsed to only the findings it had just promoted.

### The measurement that proved it

Feeding the pipeline **pure coin-flip labels** — `is_real` random, zero information, no
relationship to any finding — over 400 simulated scans of 44 findings each:

| | in-sample precision@10 |
|---|---|
| v1 baseline | 0.503 |
| v2 "recalibrated" | **1.000** |
| mean delta | **+0.497** |
| v2 ≥ v1 | **400 / 400 trials** |
| v2 reached exactly 1.000 | **400 / 400 trials** |

A metric that reports a 50-point win on random noise cannot evidence a win on real labels. Had
this shipped, the headline number would have been guaranteed positive before a single human was
hired, and the first Q&A question about methodology would have taken it apart.

### The fix

1. **Held-out evaluation** (`metrics.split_summaries`, wired in `pipeline.results`). Labels are
   split in half *by finding*; v2 is rebuilt from the fit half only; both rankings are scored on
   the eval half neither has seen. Splitting by finding rather than by rater matters — holding
   out individual raters leaks the finding's verdict through the remaining ones.
2. **Condensed lists** (`metrics._condense`). Unlabeled findings are dropped *before* truncating
   to `k`, so both rankings share one judged pool and one denominator. This is the standard
   treatment for incomplete relevance judgments.
3. **`holdout_k`.** A trap found while validating the fix: `precision@k` is rank-invariant once
   `k >= n_judged`. With a 10-finding eval half, held-out precision@10 read exactly 0.000 on
   *signal* as well as noise, because the top 10 of 10 is every permutation. The held-out k is
   now half the eval set. A metric that cannot move is broken, not conservative.
4. **Average precision normalisation.** Was `total / hits_found`, which scored "one real bug
   ranked first, nine missed" identically to "all ten ranked perfectly" — both 1.0. Now
   normalised by `min(R, k)`, restoring the recall term the metric existed to provide.

### After the fix

| labels contain | in-sample delta | **held-out delta** | held-out AP delta |
|---|---|---|---|
| noise | +0.425 (400/400 "wins") | **+0.003** (43 win / 38 lose) | +0.002 |
| signal | +0.396 (400/400 "wins") | **+0.139** (214 win / 8 lose) | +0.120 |

Flat and symmetric on noise, positive on signal. The metric can now legitimately fail, which is
what makes it worth reporting.

Locked in by `tests/test_metrics.py::TestHoldoutIsNotCircular`, which asserts the in-sample
number *does* inflate on noise (if it stops, the recalibration has silently stopped using labels)
and that the held-out number *does not*.

### What this changes about the story we can tell

On the seed fixture the honest held-out result is a **tie, 1.000 vs 1.000** — not the `+0.10`
recorded in §11.8. The simulated raters confirm 90% of findings, so there is almost nothing for a
recalibration to fix; the earlier gain was mostly the artifact.

That is a fixture problem rather than a product problem, and it is better understood now than at
17:30. It also sharpens what round 2 is for: the fresh-panel preference is a genuinely
independent measurement and remains the primary result. The ranking metric is now a defensible
supporting number instead of a circular one.

Both figures are shown on the dashboard. The in-sample one is kept, under the heading "In-sample
diagnostic — not evidence", because a judge who asks "what if you score v2 on its own labels?"
should find the number already on the page with the caveat attached.

---

## 12.6 Concurrent participants all claimed the same assignment slot

Found by reading `_claim_slot` in `app/main.py` and then reproducing it with real threads. Second
only to §12.5 in severity, and it would have burned the entire round-1 budget.

### What was wrong

`_claim_slot` bound a participant to a slot with a read-then-write:

```python
free = SELECT ... WHERE participant_id IS NULL ORDER BY slot   # every caller reads first
free.participant_id = participant_id                          # then every caller writes
```

Terac participants do not trickle in. They arrive in a burst the moment an opportunity goes live,
so every request runs that `SELECT` before any of them has committed a write. All of them see the
same lowest free slot.

### Measured, not theorised

Twelve threads released simultaneously against the real database, eight trials, comparing the old
implementation with the fix:

| implementation | worst trial |
|---|---|
| old, read-then-write | **12 participants on one slot, 11 of 12 slots never claimed** |
| new, conditional UPDATE | 0 shared, 0 unclaimed |

Not a narrow race — a near-total collapse under the exact arrival pattern Terac produces.

### Why it would have been expensive and invisible

Nothing errors. Every participant sees a working task page, completes it, and gets paid.

What breaks is *coverage*. `triage.assign_round1` exists to guarantee every finding gets the same
number of raters give or take one, because "confirmed = strict majority of 3" is undefined
otherwise. In the worst case above, 11 of 12 participants judge the same five findings — so those
five collect 33 raters, the other fifteen collect none, and 55 of the 60 judgments we paid for
tell us nothing we did not already know from the first three.

`summarize_labels` would then return verdicts for five findings, `precision_at_k` would compute a
denominator of five, and the dashboard would render a plausible-looking number. There is no
symptom to notice at 16:00.

### The fix

A conditional `UPDATE ... WHERE participant_id IS NULL` on a specific slot, so exactly one writer
wins; the losers see `rowcount == 0` and try the next slot, bounded by the slot count. Same
compare-and-swap shape already used to claim a paid `Order` in `_process_whop_event` (§12.1) — the
pattern was there, this path just had not been converted.

The retry path also re-reads the slot for the *same* participant id, so a phone user double-tapping
the task link gets their existing slot back instead of consuming a second one.

Pinned by `tests/test_slot_claiming.py`, which drives real threads through a real database. The
tests were verified to fail against the old implementation before being kept — a concurrency test
that passes either way is worse than no test, because it certifies the bug.

---

## 13. Third verification pass — adversarial review of the whole codebase

Four parallel reviews (Terac client, agents and workflows, FastAPI routes and security, bug sources
and scanner) plus a self-review of the experiment-critical logic. Every claim below was reproduced
before it was fixed; a handful of reported issues were **not** reproducible and are recorded as such
in §13.9, because a review's confident wrong answer costs as much as a bug.

Two findings arrived independently from two reviews — the `_claim_slot` race (§12.6) and the
unauthenticated money endpoints — and both had already been fixed. Independent rediscovery is the
strongest evidence available that those two were real.

### 13.1 The destructive-action guard could be defeated by ordinary DOM churn

The worst defect found, because it is the only one whose victim is someone other than us.

`_safe_targets` screened the element at DOM index *i*, then `_journey_interact` **re-queried the
DOM** and clicked `elements[i]`, with only a bounds check in between. Any re-render between the two
snapshots shifts the indices, and the element clicked is then one whose label was never screened.

Reproduced in a real browser against a fixture with a two-button cookie banner. Index 3 was screened
as `About us`; after the banner unmounts, index 3 **is** `Delete account`. The old code would have
clicked it, having already logged `Skipping destructive control: Delete account`.

Four further independent holes in the same function, each reproduced:

| Hole | Input that defeated it |
|---|---|
| Only the first non-empty label source was screened | `<button aria-label="cta-42">Delete my account</button>` |
| `href` was never screened for intent | `<a href="/items/42/delete">Trash</a>` |
| `javascript:` was not in the blocked schemes | `<a href="javascript:deleteAll()">Continue</a>` |
| The label was truncated to 80 chars *before* screening | a long consent label with `delete` at char 92 |
| Origin check was `startswith`, not an origin compare | `https://example.com.evil.com/x`, `//evil.com/x` |
| Vocabulary was English-only | `Supprimer`, `Löschen`, `删除`, `Удалить`, and `Log out` |

`Log out` deserves its own line: not data loss, but it invalidates the session, so every later
finding is captured logged-out while its evidence text claims to describe a logged-in flow.

**Fix.** Screen the *union* of every label source plus the href, untruncated; compare parsed
origins; block `javascript:`/`data:`/`blob:`; extend the vocabulary; and fingerprint everything the
decision was based on into a `safety_key` that is re-derived and compared immediately before the
click, so a shifted index is skipped rather than clicked. Pinned by
`tests/test_destructive_safety.py`.

### 13.2 A paid judgment could be silently discarded, and votes could be forged or inverted

Four separate defects on the two routes that receive work we have already paid for.

* **`POST /t/r1` with no assignment returned the thank-you page.** The loop ran over
  `assignment.finding_ids if assignment else []`, so a missing assignment stored nothing, left
  `saved == 0`, and still rendered "That's everything. Thank you." Reachable from an unknown
  `scan_id`, and from any participant who never claimed a slot.
* **One malformed field discarded the whole submission.** `severity=int(severity)` ran inside
  `session_scope`, which rolls back on any exception, so a single bad value threw away every good
  label posted alongside it and returned a 500.
* **`is_real` had no domain validation.** A value outside `IsReal` was stored and then read as
  "not confirmed" by `CONFIRMING_ANSWERS`, so a mangled POST could silently suppress a finding.
* **`POST /t/r2` recorded a vote with no assignment, and re-derived the side at POST time.** The
  first is ballot stuffing on `preference_share_v2`, the headline result, by anyone holding a scan
  id — which every participant has, in their task URL. The second inverts the vote whenever the
  rendered page and the stored assignment disagree about which report was on the left.

**Fix.** Refuse with a 409 and an explicit "we could not record this, do not resubmit, you will
still be paid" page rather than ever showing success for work we did not store; validate each label
through the `HumanLabel` model that already declares the domain, skipping only the bad row and
surfacing the count to the participant; require an assignment for a round-2 vote; and cross-check a
hidden `left_version` against the assignment, refusing on disagreement rather than recording a
possibly-inverted preference. Pinned by `tests/test_paid_judgments.py`.

This is also the fix that **found a real bug in our own rehearsal**: see §13.8.

### 13.3 The seed fallback's placeholder image was corrupt

`_PLACEHOLDER_PNG` was a hand-written hex literal whose IDAT chunk declared 13 bytes with 12
present, so every chunk after it was misaligned. Decoders reject it. `file(1)` reports a valid PNG
because it parses only IHDR, which is why it survived a casual check.

Consequence: on the one path that exists to protect the two Terac rounds when everything else has
failed, all 22 seeded findings carried non-empty screenshot URLs, so the template rendered
`<figure>` elements and every participant saw two broken images.

The docstring's claim that the placeholders "say so on their face" was also false — a 1×1 pixel says
nothing, and participants never see the `seed` label, which appears only on the dashboard and report.

**Fix.** A generated SVG, valid by construction and carrying legible text ("SAMPLE — not a live
capture"), verified to serve as `image/svg+xml`. Fixture parsing is now checked in `available()` and
tolerant per row, because this is the path whose job is to work when everything else is broken, on a
file someone will hand-edit under time pressure.

### 13.4 A round launch was not idempotent, and participant counts were unbounded

`launch_round1` and `launch_round2` created and launched an opportunity unconditionally. Every caller
above them can retry: the Render task carries `max_retries=2`, the Recruiter agent can be
re-prompted, and the operator endpoint is a POST someone can double-click. Each retry hired a second
panel for the same scan.

Separately, `num_participants` reached these functions from an LLM tool call and was passed straight
through as a multiplier on real money. And because the code read `num_participants or default`, a
request for **zero** participants was treated as absent and hired the full default panel of twelve.

**Fix.** `_launched_round` keys idempotency on a *launched* opportunity id — not the existence of a
Round row, so a run that died between inserting the row and creating the opportunity can still
retry. Counts are bounded by `MAX_PARTICIPANTS_PER_ROUND`, and a non-positive request is refused
rather than coerced. Pinned by `tests/test_money_guards.py`.

The same `or`-treats-zero-as-absent bug was in the scanner: `PlaywrightSource(max_steps=0)` — the
natural spelling for "load the page, touch nothing", which is what you reach for on a sensitive
target — performed twelve clicks instead of none.

### 13.5 Nothing refused to sell a finding with no evidence

`ReplayQASource` never sets `screenshot_before_url` or `screenshot_after_url`, and it is documented
as the *preferred* source. `is_public_base()` inspects the base URL *string*, so it cannot detect
that no evidence exists, and the launch passed cleanly.

The task page then promises "two screenshots" and renders none. The only honest answer left is
"Can't tell from this evidence", which `CONFIRMING_ANSWERS` excludes — so every category falls below
the 30% threshold in `confirmation_rate_by_category` and v2 is recalibrated on an artifact of
missing evidence, from a round we paid twelve people for.

A related hole: evidence URLs are absolute and written at capture time, so a scan that ran before
`PUBLIC_BASE_URL` pointed at the public host has `localhost` frozen into those rows.
`is_public_base()` checks only the *current* setting and passes — precisely the 13:20 failure it
exists to prevent.

**Fix.** `prepare_round1` excludes findings with no screenshot and raises if that empties the set;
`launch_round1` additionally validates the *stored* URLs with a new `is_public_url`, not just the
current setting.

### 13.6 One root cause became thirteen findings and ~39 paid judgments

There was no deduplication anywhere. A site-wide console error or a 404 on a shared stylesheet was
emitted once by the load journey and again by each of the twelve interaction steps, producing up to
13 findings with byte-identical evidence. With a budget of ~20 findings at 3 raters each, most of the
60 paid judgments went on re-answering one question — and `confirmation_rate_by_category` then
decided whether to drop an entire category based on a single duplicated root cause.

Measured on a reconstruction: 14 findings → 2, and 42 paid judgments → 6.

Worse, Playwright's *own* failures were sold as product defects. A control covered by a consent
overlay produced `observed="The interaction failed: Timeout 5000ms exceeded"` at
`agent_confidence=0.3` — and because `select_for_review` orders by distance from 0.5, those
fabricated findings were selected **ahead** of genuine 5xx findings at 0.92.

**Fix.** Deduplicate on `(category, first concrete error)` before returning, carrying an occurrence
count into `observed` ("the same failure occurred on 13 of 14 pages checked"), which is itself
severity evidence for the human. Tool-side errors are logged and skipped, never emitted as findings.

### 13.7 Smaller confirmed defects

* **`int()` on an empty env var crashed at import.** `os.environ.get(name, default)` returns the
  default only when the key is *absent*; `R1_PARTICIPANTS=` in a `.env`, or a blank field in the
  Render dashboard, yields `""` and `int("")` raises. Because these are class-body expressions the
  failure is an import-time crash in every process at once. Now routed through `_int`/`_float`,
  which treat present-but-empty as absent and log a malformed value instead of dying.
* **Copying `.env.example` verbatim stopped the app from booting.** Found by running our own
  setup instructions. `.env.example` ships `DATABASE_URL=` blank on purpose — the comment above it
  says the default is sqlite — but `os.environ.get(name, default)` honours a default only when the
  key is *missing*, so a blank line set `database_url` to `""` and `create_engine("")` raised
  `ArgumentError`. Nothing started. Same present-but-empty class as the `int()` crash above; the
  string settings had been missed. Now routed through `_str`, which is applied only to settings with
  a real default — optional credentials still resolve to `None`/`""` and every consumer already
  tests them for truthiness, which was verified rather than assumed.
* **A veto on a nonexistent scan silently gated nothing.** `record_veto` accepted any `scan_id`, so
  a slightly-wrong id from a model stored a block that `open_vetoes` for the real scan never saw:
  `release_report` released, and Critic had every reason to believe it had blocked. This is the one
  governance property the crew exists to demonstrate. Now validated against both scan and finding.
* **Bursar held `scan_url`.** `docs/AGENTS.md` §4 says Bursar "@Scout with the URL"; owning the tool
  gave it a path that bypasses the room, which is exactly the dependency the delete test claims
  exists. Removed from its toolset.
* **`ensure_webhook` had no callers.** No Terac subscription was ever registered, so
  `submission.approved` never arrived and the only route for labels was a manual poll endpoint. Now
  called best-effort before the first launch — polling stays the documented fallback, so a webhook
  problem must not stop us hiring, and the already-registered 409 makes it idempotent.
* **The Whop webhook failed open.** With no secret configured, verification was skipped *silently*,
  so anyone who could reach the URL could forge `payment.succeeded` and spend our Terac credit on
  their target. Now refuses when the instance is publicly reachable. (The Terac receiver's
  equivalent branch is a documented, loudly-logged trade-off tied to subscription confirmation, and
  was left alone.)
* **A paid order could produce no scan, silently.** The order was marked `paid` before
  `create_scan`, so an `UnsafeTargetError` — the host stopped resolving between checkout and payment
  — left it `paid` with a NULL `scan_id` and nothing recording why. Now sets `status="refund_due"`
  and stores the reason, so a customer charged for nothing is visible in the data.
* **The Replay poller discarded every bug on a transient error.** `_list_bugs` returned `[]` on any
  4xx/5xx, indistinguishable from a genuine empty result, and `bugs` was reassigned every
  iteration — so one 429 on the final poll threw away ten minutes of exploration and logged
  "returning 0 bugs found so far". It now returns `None` for a failure and the caller keeps the best
  result seen.
* **An undocumented list field would have rendered to participants as a Python repr.**
  `str(bug["reproduction_steps"])` on a list yields `"['Open the homepage', 'Click Sign in']"`,
  brackets and quotes included, in the one line a participant reads to understand what was tried.
  Flattened by `_as_text`.
* **An unbounded `page.evaluate` could hang a scan forever.** `evaluate`, `is_visible` and
  `inner_text` accept no timeout and run on the page's JS thread. The origin lookup is now computed
  from `page.url` in Python, and the journeys run under an explicit `SCAN_DEADLINE_SECONDS` below the
  workflow's own timeout, so a deadline fires in-process where the browser can still be closed.
* **`browser.launch()` sat outside the `try`.** `pip install playwright` succeeds without
  `playwright install chromium`, and `available()` only checks the import, so a missing browser
  binary raised out of `scan()` and broke the protocol's "must not raise" contract.
* **`max_journeys` was read and never applied**, while the module docstring cited it as an abuse
  control from SPECS §9. A documented guard that does not exist is worse than an absent one, because
  it stops anyone from looking for it. Deleted, and the docstring corrected.

### 13.8 The validation fix immediately caught a bug in our own rehearsal

`scripts/rehearse_experiment.py` submitted `is_real="clear_no"`, which is not a member of `IsReal`
(`clear_yes | probably | no | cant_tell` — the values `t_r1.html` actually posts). Before §13.2 that
value was stored raw and only *happened* to read as non-confirming; once the route began validating,
those labels were correctly dropped.

The effect was visible and instructive: the rehearsal reported 54 of 60 judgments stored, a
confirmation rate of 1.0, and **zero** findings demoted — the two findings a simulated rater would
reject were assigned and never labelled. Corrected to `no`, the same rehearsal reports 60 of 60,
a rate of 0.9, and two findings demoted.

The script now also fails loudly if the server reports any rejected label, because a rehearsal that
returns 200 while quietly losing the judgments it exists to produce is worse than one that crashes.

**This is the honest state of the improvement metric**, from that run:

```
HELD OUT (v2 rebuilt without these labels)   IN SAMPLE (circular, diagnostic only)
  precision@5  v1 0.800  v2 0.800             precision@10  v1 0.900  v2 1.000
  AP           v1 0.876  v2 0.876
```

The naive in-sample number claims +0.100. Held out, v2 ties v1. A tie against simulated raters who
agree with the machine 90% of the time is the expected outcome and not a defect — but it is exactly
the gap §12.5 predicted, and it is the number we will quote.

### 13.9 Reported and NOT reproducible

Recorded so nobody re-fixes a non-bug.

* **"`qualify_logic: "must"` is invalid on `pick: "any"`."** This was in `CLAUDE.md`, in
  `CONTRIBUTING.md` as the worked example of *citing a verified fact*, in a comment in
  `scripts/probe_terac.py`, and — worst — asserted as a test. The live docs say the opposite: `must`
  is "valid with `one` or `any`", and the distinction is semantic (this exact answer vs at least one
  of a group). The constraint never existed. It has been corrected in all four places, and the test
  now asserts the real invariant (`qualify_logic` is in the documented enum, and no screener rejects
  its entire population). Already noted in §8.1 and §10.2; the correction had not propagated out of
  RESEARCH.md into the file that is loaded every turn, which is the actual lesson.
* **Dialog handling was reported as a hang risk.** It is correct as written: with no `dialog`
  listener registered, Playwright auto-dismisses every dialog, which also makes a
  `confirm("Delete this?")` resolve to `false`. Left alone, and the reliance is now commented, since
  adding a listener later would introduce both a hang and a destructive-click path.
* **Screenshot filename collisions.** Not possible: `_shot` suffixes every name with a UUID.
* **A failed screenshot rendering as a broken image.** Not possible: `_shot` returns `""` and every
  `<img>` in `t_r1.html` is guarded.
* **`_flag` mishandling `"false"`.** Correct as written; `bool("false")` is never evaluated.
