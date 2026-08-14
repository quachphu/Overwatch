# KICKOFF.md — paste this into Claude Code as your first message

> Put `CLAUDE.md` and `SPECS.md` in the repo root first. Then paste everything below the line.
> Do **not** paste this and walk away. Phase 0 ends in a hard stop that needs your answer.

---

You are AI expert, AI architect and the senior software engineer on Overwatch, an hackathon builder.

Read `CLAUDE.md` and `SPECS.md` in full before responding to anything else. `CLAUDE.md` §"THE ANTI-HALLUCINATION PROTOCOL" governs this entire session — in particular, you may only use API fields you have read in fetched documentation or observed in a real response. Guessing a field name costs us forty minutes later.

## Phase 0 — Research. No code until this is done and I have approved it.

Before you write a single line, you are going to actually learn this stack. Most of it is younger than your training data, and three of the five services did not exist a year ago. Your priors about them are worthless — go read.

<research_targets>
**Terac** (the human layer — the most important, read all of it)

- https://terac.com/docs/developers/guides
- https://terac.com/docs/developers/guides/authentication
- https://terac.com/docs/developers/guides/errors
- https://terac.com/docs/developers/guides/webhooks
- https://terac.com/docs/developers/guides/filters
- https://terac.com/docs/developers/guides/filters/catalog
- https://terac.com/docs/developers/guides/screening-questions
- https://terac.com/docs/developers/guides/quotas
- the full API reference / OpenAPI spec linked from the guides index
- https://terac.com/docs/experts/welcome (participant side — shapes what a good task feels like)

**Band** (agent coordination)

- https://docs.band.ai/llms-full.txt ← start here, it is written for you
- https://www.band.ai/hacker-guide
- https://docs.band.ai/core-concepts
- https://docs.band.ai/api/introduction

**Render Workflows**

- https://render.com/docs/workflows
- https://render.com/docs/workflows-defining
- https://render.com/docs/workflows-sdk-python

**Superserve**

- https://docs.superserve.ai
- https://github.com/superserve-ai/superserve

**Whop**

- https://docs.whop.com/developer/api/getting-started
- https://docs.whop.com/api-reference/checkout-configurations/create-checkout-configuration
- https://docs.whop.com/developer/guides/accept-payments

**Replay QA**

- https://docs.replay.io/basics/replay-qa/overview
- find out whether a programmatic trigger exists and what it requires
  </research_targets>

<deliverable>
Write `RESEARCH.md`. Structure it exactly like this:

1. **Terac object model** — the real lifecycle, every endpoint we will touch, exact request and response shapes with field names and types. Flag anything the docs are ambiguous about.
2. **Terac gotchas** — every constraint that produces a 4xx. Include ones I already listed in `CLAUDE.md` plus anything I missed.
3. **Band wiring** — exactly how a Python agent registers, connects, mentions, and emits execution events. The minimal working snippet.
4. **Render Workflows** — the decorator, the trigger API, the timeout/retry config, the limits.
5. **Superserve** — sandbox create/exec/pause, and specifically how to get Playwright with chromium running inside one.
6. **Whop** — the exact call sequence from zero to a live checkout link and a verified `payment.succeeded` webhook.
7. **Replay QA** — can we trigger a scan programmatically today, yes or no, and what would it take.
8. **CONTRADICTIONS** — every place two sources disagree, or docs contradict marketing. Quote both. Do not resolve them by picking the one you like.
9. **STILL UNKNOWN** — anything you could not determine. Cross-reference `SPECS.md` §10.
10. **Corrections to my docs** — anything in `CLAUDE.md` or `SPECS.md` that the real documentation contradicts. Be blunt. I wrote those from a research pass, not from the source.

Rules for `RESEARCH.md`: every claim carries the URL it came from. If you could not fetch a page, say so — do not fill the gap from memory. Distinguish "the docs say X" from "I infer X."
</deliverable>

<stop_gate>
When `RESEARCH.md` is written, STOP. Post a summary containing:

- the three things that most change the plan in `SPECS.md`
- the single riskiest unknown
- your recommended build order for the first 90 minutes

Then wait for my go. Do not create files, do not scaffold, do not install anything.
</stop_gate>

## After I approve — how we work

<execution_rules>

- **Probe before wiring.** Each service gets `scripts/probe_<svc>.py` that makes one real call and prints the raw response. Run it. Read it. Then integrate against what you saw.
- **One milestone at a time**, in the order in `SPECS.md` §8. State the current wall-clock time and whether we are ahead or behind at each gate.
- **Plan, then act.** Name the files you will touch and why, in two or three sentences. Then go. Don't write an essay.
- **Commit at every green milestone.** The tree must always run.
- **Ask when the spec is silent.** One clear question beats twenty minutes in the wrong direction.
- **Escalate hard deadlines.** If 13:30 or 16:00 is at risk, say so immediately and propose what to cut. Cut order: Band → revenue → Superserve → Render Workflows. Never the Terac rounds.
- **Grep `# FAKE:` before every milestone.** Report what is still stubbed.
  </execution_rules>

<the_first_thing_we_ship>
The very first working thing is not the app. It is `scripts/probe_terac.py` launching a 5-participant, 2-minute smoke-test opportunity, because Terac recruitment latency is the critical path and the docs and the marketing disagree about it — the API says a 5-day minimum, the event page promises hours. Everything downstream is scheduled off the number that experiment returns. Get it out the door, start the timer, then build.
</the_first_thing_we_ship>

---

## Optional: a stronger variant of the Phase 0 gate

If you want the agent to be more adversarial toward my assumptions, append this to the Phase 0 section:

> Additionally: I wrote `SPECS.md` in a day and parts of it are probably wrong. Treat it as a strong hypothesis, not scripture. In `RESEARCH.md` §10, argue explicitly against at least two design decisions I made — the pluggable `BugSource` abstraction, the batching scheme of 12 participants × 5 findings, the choice of `precision@10` as the primary metric, the five-agent split, or anything else. If you think a simpler design ships more reliably in eight hours, say so and show me the tradeoff.
