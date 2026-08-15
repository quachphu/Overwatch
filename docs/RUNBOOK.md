# RUNBOOK.md — for you, not the agent

`CLAUDE.md` and `SPECS.md` are what the coding agent reads. This is what **you** follow. Keep it open on your phone.

---

## Tonight (Friday)

Account setup and reading docs is not building the project — every hackathon allows it. Do all of it now so tomorrow is pure code.

- [ ] **Band** — app.band.ai, register 5 agents. **Copy each API key immediately; shown once.** Build `agent_config.yaml`.
- [ ] **Render** — Postgres + Web Service (**paid instance**, free tier spins down and drops webhooks) + Workflow service. Deploy a health endpoint. Confirm it loads from your phone on cell data, not wifi.
- [ ] **Superserve** — account, `ss_live_…` key.
- [ ] **Whop** — company created, `biz_…` id + API key. **Check if payouts need KYC. If yes, start it tonight** — discovering this at 15:00 kills the revenue track.
- [ ] **Replay** — account at qa.replay.io. Run one scan on a sample buggy app so you know the output shape before you need it.
- [ ] `pip install -r requirements.txt` — resolve conflicts tonight, not at 11:00.
- [ ] Write both task page HTML templates with fake data. Pure frontend, zero API dependency, works regardless of what tomorrow reveals.
- [ ] `timedatectl` / NTP sync. Terac's HMAC window is 300 seconds and clock skew produces a signature failure that looks like a code bug.
- [ ] Sleep. Seriously. Eight hours of decisions on four hours of sleep is a worse trade than any prep you'd do instead.

---

## Saturday

**08:30** Doors, breakfast, check-in. QR ticket ready.

**09:15 — Opening ceremony.** Grab the attendee doc. Three things matter:
- Terac API key → `.env` immediately
- **"Best Use of Terac" criteria** — announced today, may reshape priorities
- Credit budget per team → sets `num_participants` on both rounds

**09:45 — Sponsor stage, attendance required.** Listen specifically for Replay's API story.

**10:20 — Booth Q&A. Go to Terac first, then Replay.** Ask in this order:

*Terac:*
1. Is the 5-day `expected_days_to_complete` minimum waived for hackathon keys?
2. Realistic minutes-to-first-completion, 3-min general-population task?
3. Credit budget per team?
4. REST + webhooks, or MCP + polling?
5. Exact value format for `reference--has_not_taken_study`?
6. Feasibility pricing — instant, or human-priced out of band?

*Replay:* can I trigger a scan programmatically today — API key, MCP, or neither?
*Render:* Workflows enabled on hackathon credits?
*Whop:* payout verification for a same-day account?

**10:45 — `python scripts/probe_terac.py --launch`.** Before any other code. Write T0 on your hand.

**11:00 — Replay decision gate.** API access or not. Switch to Playwright and never revisit.

**12:30 — Spine gate.** ≥15 real findings with public screenshot URLs. Open one in incognito on your phone to prove it's actually public. If the gate fails, switch to a pre-scanned fallback app and move on — do not let the scanner eat the day.

**13:30 — Round 1 launch. Hard deadline.** Ship whatever findings exist.

**15:00 — Crew gate.** Five agents in one room, `Emit.EXECUTION` on, one full handoff visible in the Band console.

**16:00 — Round 2 launch. Hard deadline.** Partial labels are fine. A round-2 launch at 16:00 with imperfect input beats a perfect one at 17:30 that never fills.

**16:00–17:30 — Build the chart.** Revenue (Whop) was cut — out of scope for the scored rubric, second in the cut order below, and ripped out entirely once it started costing more debugging time than it was worth (`RESEARCH.md` §13.22). Time that would have gone to selling scans goes to the dashboard and the pitch instead.

**17:30–18:20 — Record the video twice.** Venue wifi will not be your friend at 20:20.

**18:20–18:45 — Buffer. LOCK AT 18:45.**

---

## Cut order

Under time pressure, in this order: **Band → revenue (already cut, see `RESEARCH.md` §13.22) → Superserve → Render Workflows.**

**Never cut the two Terac rounds.** They are roughly two-thirds of the overall rubric and the entire reason this event exists.

---

## Before you call anything done

```bash
make fakes        # must print "clean"
```

An unmarked stub that survives to the demo is a lie told to judges, and this panel asks questions.

---

## Demo script — 2 minutes

1. **10s** "Overwatch is a company with no employees. It sells QA. The only humans involved are the ones it hires, per job, through Terac."
2. **25s** Paste a URL live. Cut to the Band room — Scout posts findings, Triage @mentions Recruiter, Recruiter launches Terac. Real agents on camera.
3. **25s** Terac dashboard, real submissions arriving. Show one evidence bundle exactly as a participant saw it.
4. **50s** The chart. Lead with preference share and its interval, then held-out precision v1→v2. **State n. State that round 2 excluded round-1 participants by filter. Say the word "held-out" out loud** — it pre-empts the sharpest question available to this panel, which is whether v2 was scored on the labels that built it. It was not, and the dashboard shows the in-sample number separately to prove we know the difference.
5. **10s** "Take Band out and the pipeline halts after Scout. Take Terac out and there's no improvement at all — just another bug list nobody trusts."

---

## If the result is negative

Report the real number. "Human input moved held-out precision from 0.55 to 0.52, and here's our read on why" is a stronger submission than a fabricated lift, and the judges — one pair of whom builds multi-agent eval environments for a living — will find a fabricated one in Q&A. An honest negative with clean methodology still scores on "what you built" and "use of human input."

Expect the held-out number to be *smaller* than the in-sample one, and possibly flat. That is what a held-out number is for. The failure mode to avoid under time pressure is re-seeding the split or widening the fit half until the figure improves — that is fabrication with extra steps, and docs/DECISIONS.md 010 exists to make it a visible reversal rather than a quiet tweak.
