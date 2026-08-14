# Contributing

## Setup

```bash
make install          # venv + dependencies + ruff
playwright install chromium
cp .env.example .env
make dev              # http://localhost:8000
```

`make help` lists every target.

## Before you push

```bash
make gate             # unmarked stubs + lint + tests
make ci               # the above plus the live-server webhook test
```

CI runs lint, tests on Python 3.11 and 3.13, `make e2e`, the stub check, and the landing-page
build. `make ci` reproduces it locally so a red build does not need a push to diagnose.

## Conventions

**Never call an API surface you have not verified.** Only send a field you have read in live
documentation or observed in a real response. If you need one and cannot find it, stop and say
`BLOCKED: need <field> on <endpoint>, not in docs at <url>` rather than guessing. A guessed field
name becomes a 400 hours later, and finding it costs far more than asking did.

**Probe before you wire.** Every external service has a `scripts/probe_<service>.py` that makes
one real call and prints the raw response. Run it, read the actual shape, then write the
integration against what you saw.

**Mark every stub.** If you must hardcode something to keep moving:

```python
# FAKE: hardcoded until the Terac key arrives. Replace before the demo.
```

`make fakes` must print `clean` before a milestone counts as done, and CI enforces it. An unmarked
stub that survives to a demo is a lie told to an audience.

**Cite non-obvious API usage inline**, so the next reader does not have to re-derive it:

```python
# docs: https://terac.com/docs/developers/guides/screening-questions
# On pick:"one", may/must/must_one_of all collapse to the same disposition — only
# reject/review change a single-select outcome.
```

**Comment the why, never the what.** Explain a constraint, a trade-off, or a failure that a
reader could not infer from the code. Do not narrate what the next line does.

## Tests

Unit tests must not touch the network. `tests/conftest.py` redirects the database to a temporary
file before `app.db` is imported and defaults `BUG_SOURCE=seed`.

Two suites carry more weight than the rest, and changes near them deserve care:

- `tests/test_metrics.py` — the measurement *is* the deliverable. A bug here does not crash
  anything; it reports a lift that is not real. `TestHoldoutIsNotCircular` in particular pins the
  property that v2 must not be scored on the labels that built it. Read `app/metrics.py`'s module
  docstring before touching it.
- `tests/test_operator_auth.py` — the endpoints it guards spend real money on real people.

When you fix a bug, add the test that would have caught it, and say in the test's docstring what
breaks if the property is violated. A test named after a mechanism ages badly; one that states a
consequence does not.

## Money and people

Two Terac rounds hire real humans and spend real credit. Consequences of that:

- Anything reachable that spends credit is authenticated (`require_operator` in `app/main.py`).
- Every pipeline step is safe to call twice. The human loop takes hours and something will retry.
- Round 2 must exclude round 1's participants. `pipeline.launch_round2` refuses to launch if it
  cannot, because "a fresh panel" would otherwise be a false claim and the result contaminated.
- The scanner drives a browser through somebody else's live site. `is_destructive()` in
  `app/security.py` gates interactions; widen it rather than narrow it if you are unsure.

## Reporting a real result

If the improvement metric does not improve, report the real number. A truthful negative result is
worth more than a fabricated lift, and a fabricated one does not survive a question.
