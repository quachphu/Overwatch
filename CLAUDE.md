# CLAUDE.md

Project memory for Overwatch. Loaded every turn — keep it tight. Detail lives in `SPECS.md`, research findings in `RESEARCH.md`.

---

## What we are building

**Overwatch** — an agent-run QA company. A customer pastes a URL and pays. A crew of autonomous agents scans the app for bugs, then **autonomously hires real humans through the Terac API** to verify which findings are real and which matter. Those human labels recalibrate our triage, producing a measurably better report — proven by a second, fresh panel of humans who never saw round one.

Built for the Zero-Human Company Hackathon (Terac, Aug 15 2026, SF). Build window is **10:45–18:45, eight hours.** Submissions lock at 18:45 and do not reopen.

**The scoring reality that drives every decision:** the overall rubric is Project improvement 40%, What you built 35%, Use of human input 25%. Roughly two-thirds of the score is the human-in-the-loop measurement, not the app. When time is short, protect the two Terac rounds and cut everything else.

---

## THE ANTI-HALLUCINATION PROTOCOL

This is the most important section in this file. Violating it wastes hours we do not have.

### Rule 1 — No unverified API surface. Ever.

You may only call an endpoint, pass a field, or use a parameter that you have **personally read in fetched documentation during this session**, or **observed in a real response**. Not "it's probably called that." Not "this is the standard REST pattern."

If you need a field and cannot find it in the docs:
1. Stop.
2. Say: `BLOCKED: need <field> on <endpoint>, not in docs at <url>.`
3. Propose the smallest experiment that would resolve it (usually a curl).
4. Wait.

Do not guess and move on. A guessed field name at 14:00 becomes a 400 at 16:00 that costs forty minutes to find.

### Rule 2 — Verify before you wire.

Every external API gets a standalone `scripts/probe_<service>.py` that makes one real call and prints the raw response. Run it. Read the actual shape. **Then** write the integration against what you saw. Never write the client and the caller in the same pass before a single real request has succeeded.

### Rule 3 — Mark every fabrication.

If you must stub, mock, or hardcode anything to keep moving, mark it:

```python
# FAKE: hardcoded until Terac key arrives. Replace before demo.
```

Grep for `# FAKE:` before every milestone. An unmarked stub that survives to the demo is a lie told to judges.

### Rule 4 — Cite non-obvious API usage inline.

```python
# docs: https://terac.com/docs/developers/guides/screening-questions
# On pick:"one", may/must/must_one_of all collapse to the same disposition — only
# reject/review change a single-select outcome.
```

### Rule 5 — Never silently widen scope.

If a task requires something not in `SPECS.md`, say so and ask. Do not add a caching layer, an auth system, a retry framework, or a config abstraction because it seemed right. We have eight hours.

### Rule 6 — UNKNOWNS stay unknown.

`SPECS.md` has an **UNKNOWNS** section listing things only confirmable at the sponsor booths. Do not fill them in with plausible values. If a task depends on an UNKNOWN, implement behind a constant at the top of the file with a `# UNKNOWN:` comment so it is one edit to correct.

### Rule 7 — Report failure honestly.

If the improvement metric does not improve, report the real number. A truthful negative result scores better with this judging panel than a fabricated lift, and fabricated numbers are trivially caught in Q&A.

---

## Verified stack

Verified against live docs on 2026-08-14. Do not substitute alternatives without asking.

| Purpose | Package / service | Note |
|---|---|---|
| API + task pages | `fastapi`, `uvicorn[standard]` | Server-rendered Jinja. No React build. |
| Agent framework | `langgraph`, `langchain-openai` | |
| Agent coordination | `band-sdk[langgraph]` | **install name `band-sdk`, import name `band`** |
| Durable orchestration | `render_sdk>=0.6.0` | Render Workflows, public beta |
| Sandbox | `superserve` | key format `ss_live_…` |
| Payments | `whop_sdk` | needs `company_id` (`biz_…`) |
| Human layer | Terac REST v2 via `httpx` | **no SDK exists** — hand-rolled client |
| Browser fallback | `playwright` + chromium | only if Replay API is unavailable |
| DB | `sqlalchemy`, `psycopg[binary]` | Render Postgres |
| Types | `pydantic` v2 | every external payload gets a model |

**Terac base URL:** `https://terac.com/api/external/v2`
**Auth:** `Authorization: Bearer $TERAC_API_KEY`
**Rate limit:** 100 req/min per key.

---

## Known API gotchas — memorize these

**Terac**
- Every `POST` needs a JSON body and `Content-Type`, even path-only actions. A bodyless POST returns `415`. Send `-d '{}'`.
- `expected_days_to_complete` has a documented **minimum of 5**, default 7. Send 5.
- `cross_quotas` without `screening_questions` → `BAD_REQUEST`.
- `qualify_logic` is one of `may | must | must_one_of | reject | review`. **`must` is valid on both `pick: "one"` and `pick: "any"`** — the difference is semantic, not a validity rule: on multi-select `must` means *this exact answer* is required, `must_one_of` means *at least one* of a group. (An earlier version of this file said `must` was invalid on `pick: "any"`; it is not. See RESEARCH.md §8.1.) On `pick: "one"`/`boolean`, `may`/`must`/`must_one_of` all collapse to the same disposition and read back as `must`, so only `reject`/`review` change a single-select outcome.
- Launching an already-active opportunity → `409`. Treat as success; it makes retries idempotent.
- Webhook signature is `base64(HMAC-SHA256(secret, timestamp + RAW_BODY))`. Parsing and re-serializing the JSON changes the bytes and breaks it. Sign raw bytes.
- Dedup on `X-Event-ID` (stable across retries). Order by `X-Timestamp`.
- Subscribe to `submission.approved` **only**. Adding `submission.status.change` means every approval arrives twice, and a submission emits ~5 status changes.
- Webhook `target_url` must be public https, redirects are never followed, 10s timeout. ACK `2xx` first, then work.

**Band**
- One live WebSocket per `agent_id`. A second process on the same ID **silently drops the first, with no error on either side.** Every agent needs its own registration, UUID, and key.
- Agents only see messages they are `@mentioned` in. Humans in the room see everything.
- Without `features=AdapterFeatures(emit={Emit.EXECUTION})` the room contains chat only — no tool calls, no reasoning. Our judging evidence depends on this being on.
- Never name an agent "Assistant", "Bot", or "Agent" — LLMs read those as role tokens and routing degrades.

**Render**
- Free-tier services spin down. The webhook receiver must be on a paid instance or we lose Terac and Whop deliveries.
- Ephemeral disk wipes on redeploy. Screenshots must go to persistent storage or both Terac rounds break.

**Whop** (verified against `whop_sdk` 0.0.41's generated types — see RESEARCH.md §12.1)
- Base URL is `https://api.whop.com/api/v1`. **Not v5.** Sandbox is `https://sandbox-api.whop.com/api/v1`.
- `plan_id` and `plan` are **mutually exclusive** on `POST /checkout_configurations`. Send one.
- Use `purchase_url` from the create response. `https://whop.com/checkout/{plan_id}` is a fallback.
- `metadata.order_id` passes through to the `payment.succeeded` webhook — that is our join key. The payment's `checkout_configuration_id` is the backup join key.
- Webhooks are **Standard Webhooks**, not Terac's scheme: `base64(HMAC-SHA256(secret, "{webhook-id}.{webhook-timestamp}.{raw body}"))`, header `webhook-signature: v1,<sig>`, 5-minute tolerance, multiple space-delimited signatures during rotation.
- Branch on the envelope's `type`, never on `status` — the spec example says `"paid"` and the guide says `"succeeded"`.
- Sandbox and production are separate. Real revenue requires production.

**Replay QA** (RESEARCH.md §12.3)
- Live REST API: `https://qa.replay.io/api/v1`, `Bearer lqa_…`. `POST /projects` requires `name` + `target_url`.
- **No endpoint has a documented response schema.** Bug field names are inferred from the `webhook_url` payload docs. Never assume a response field without a probe.

**Render** (RESEARCH.md §12.2)
- `render_sdk.task` and `render_sdk.start` are **deprecated**. Use `app = Workflows()`, `@app.task(retry=…, timeout_seconds=…)`, `app.start()`. The signatures differ — module-level takes `options=Options(…)`.
- No durable sleep, no wait-for-external-event. A workflow cannot block on a webhook.

---

## Working agreement

- **Plan before editing.** State the files you will touch and why, then act.
- **Small commits, working tree always runnable.** `git commit` after every green milestone.
- **Read before writing.** Never edit a file you have not read this session.
- **One thing at a time.** Do not refactor while implementing.
- **Time checks.** At each milestone, state the wall-clock time and whether we are ahead or behind the schedule in `SPECS.md` §8.
- **Ask when the spec is silent.** One clear question beats twenty minutes of wrong direction.

## Commands

```bash
make help           # every target, explained. Start here.
make dev            # uvicorn app.main:app --reload
make agents         # launches all 5 Band agents in tmux panes
make probe SVC=terac  # scripts/probe_terac.py — one real call, raw output
make test           # pytest, fast unit only
make lint           # ruff check
make fakes          # grep -rn "# FAKE:" app/ — must be empty before demo
make gate           # fakes + lint + test. Run before every milestone.
make ci             # what CI runs, locally
```

Tool config lives in `pyproject.toml` (ruff, pytest). `requirements.txt` stays the installer of
record — docs/DECISIONS.md 011.

## The metric is held out, not in-sample

Report v2 is fitted to the round-1 labels, so scoring it on those same labels measures our own
fitting — on pure coin-flip labels it "improves" by 50 points in 400/400 trials. `evaluate()`
therefore reports a **held-out** figure: labels split by finding, v2 rebuilt from one half, both
rankings scored on the other. That is the number the dashboard leads with and the only one to
quote. The in-sample figure is kept on the page labelled as a diagnostic.

Do not "fix" a disappointing held-out number by widening the fit half or re-seeding the split.
docs/DECISIONS.md 010 and RESEARCH.md §12.5.

## Layout

```
app/
  main.py            FastAPI: ingress, webhooks, task pages, dashboard
  clients/           terac.py  band.py  whop.py  superserve.py
  sources/           base.py (BugSource protocol)  replay.py  playwright.py
  agents/            scout.py triage.py recruiter.py bursar.py critic.py
  models.py          pydantic + sqlalchemy
  metrics.py         precision@10, Wilson interval
  templates/         r1_verify.html  r2_compare.html  report.html
scripts/             probe_*.py
workflows/           scan_workflow.py
```

## Definition of done for any task

1. Runs against the real API, not a mock.
2. No unmarked `# FAKE:`.
3. Failure path handled — what happens on 4xx, on timeout, on empty result.
4. If it touches an external payload, there is a Pydantic model for it.
5. You stated what you verified and what you assumed.
