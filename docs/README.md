# Documentation index

Read in this order depending on why you are here.

## "What is this and does it work?"

1. [`../README.md`](../README.md) — what Overwatch is, how to run it, what it measures.
2. [`SPECS.md`](SPECS.md) — the full technical specification: data model, the two Terac rounds,
   the metric, the build schedule and its gates.
3. [`../RESEARCH.md`](../RESEARCH.md) §12.5 — the most important correction we made. The headline
   metric was circular; the section shows the simulation that proved it and the fix.

## "Why is it built this way?"

- [`DECISIONS.md`](DECISIONS.md) — numbered architectural decisions, each with the alternative
  that was rejected and why. Cited from code by number.
- [`../RESEARCH.md`](../RESEARCH.md) — the verified API surface. Every external field name we send
  was read in live documentation or observed in a real response; this file records which, and
  lists what is still unverified. §11 and §12 are the corrections that only surfaced by running
  the thing.

## "I need to operate it"

- [`RUNBOOK.md`](RUNBOOK.md) — the demo script, and what to do when a step fails mid-demo.
- [`KICKOFF.md`](KICKOFF.md) — the build-day plan and the gate times.
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — environment, tests, conventions.

## "What are the agents supposed to do?"

- [`AGENTS.md`](AGENTS.md) — the five agent roles and their prompts, verbatim.
  `app/agents/prompts.py` transcribes these; if the two disagree, this file is correct.

Governance worth knowing before reading the agent code: only Recruiter may spend Terac credit,
and only Critic may lift a Critic veto. Both are enforced in `app/pipeline.py` and by which tools
each agent is handed — not in prompts, because a prompt is a request and an LLM can decline it.

## Reference

- [`terac_openapi.json`](terac_openapi.json) — the Terac v2 spec as fetched. The source of truth
  for every payload in `app/clients/terac.py`.
