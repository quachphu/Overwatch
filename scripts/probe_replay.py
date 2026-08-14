#!/usr/bin/env python3
"""One real call to Replay QA. Prints the raw response.

    make probe SVC=replay          # verify auth + read the spec, create nothing
    make probe SVC=replay -- --create https://example.com   # really start a project

CLAUDE.md rule 2. The create-project *request* is verified against Replay's own OpenAPI spec
(`required: [name, target_url]`), but the spec gives **no response schema** for any endpoint — only
prose like `description: "List of bugs"`. So the field names `app/sources/replay.py` reads out of a
bug object come from the documented `webhook_url` payload, and this probe is what confirms the
list endpoint agrees with it.

Two modes on purpose. Creating a project spends credits ("Defaults to 20 when omitted"), so the
default run only authenticates and lists, and `--create` is opt-in.

Resolves RESEARCH.md UNKNOWN 8.
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

from app.config import settings
from app.sources.replay import DEFAULT_BUDGET, POLISH_PASSES, ReplayQASource

# Documented in the create-project schema as the `webhook_url` payload. Presumed to match the
# objects returned by GET /projects/{id}/bugs — that presumption is what this probe tests.
DOCUMENTED_BUG_FIELDS = [
    "bug_id",
    "title",
    "severity",
    "description",
    "reproduction_steps",
    "expected_behavior",
    "actual_behavior",
    "replay_recording_id",
    "analysis",
    "polish_category",
]


def show(label: str, value: object) -> None:
    print(f"\n── {label} " + "─" * max(0, 76 - len(label)))
    if isinstance(value, dict | list):
        print(json.dumps(value, indent=2, default=str)[:4000])
    else:
        print(value)


async def main() -> int:
    if not settings.replay_api_key:
        print(
            "REPLAY_API_KEY is not set. Get one from Settings > API Token (starts with lqa_).",
            file=sys.stderr,
        )
        return 2

    source = ReplayQASource()
    show(
        "configuration under test",
        {
            "base_url": source.base_url,
            "key_prefix": settings.replay_api_key[:4],
            "budget": DEFAULT_BUDGET,
            "polish_passes": POLISH_PASSES,
        },
    )

    create_target = None
    if "--create" in sys.argv:
        i = sys.argv.index("--create")
        if i + 1 >= len(sys.argv):
            print("--create needs a target URL.", file=sys.stderr)
            return 2
        create_target = sys.argv[i + 1]

    async with source._client() as client:
        try:
            listed = await client.get("/api/v1/projects")
        except httpx.HTTPError as exc:
            show("transport error", f"{type(exc).__name__}: {exc}")
            return 1

        show(f"GET /api/v1/projects -> HTTP {listed.status_code}", listed.text[:2000])
        if listed.status_code == 401:
            print("\n401. The key is wrong or revoked.", file=sys.stderr)
            return 1
        if listed.status_code >= 400:
            return 1

        if create_target is None:
            print(
                "\nAuth works. Creating a project spends credits, so it is opt-in:\n"
                "  python scripts/probe_replay.py --create https://your-target.example\n"
                "Run that to learn the project-id field name and the real bug shape."
            )
            return 0

        created = await source._create_project(client, url=create_target, scan_id="probe0001")
        show("POST /api/v1/projects -> body", created)

        # The spec documents only "Created project with exploration_id and url", so which key
        # holds the project id is genuinely unknown until now.
        try:
            project_id = source._project_id(created)
        except RuntimeError as exc:
            show("project id", f"NOT FOUND — {exc}")
            print(
                "\nAdd the real key to _project_id() in app/sources/replay.py.",
                file=sys.stderr,
            )
            return 1
        show("project id", {"resolved": project_id, "keys_present": sorted(created)})

        bugs = await source._list_bugs(client, project_id)
        show(f"GET bugs -> {len(bugs)} bug(s)", bugs[:2])

        if not bugs:
            print(
                "\nNo bugs yet — exploration takes minutes. Re-run the bug listing later to "
                "confirm the field names below. The scan path polls for you."
            )
            return 0

        first = bugs[0]
        missing = [f for f in DOCUMENTED_BUG_FIELDS if f not in first]
        show(
            "bug shape vs the documented webhook payload",
            {
                "present": [f for f in DOCUMENTED_BUG_FIELDS if f in first],
                "missing": missing,
                "undocumented_extras": sorted(set(first) - set(DOCUMENTED_BUG_FIELDS)),
            },
        )
        show("mapped to RawFinding", source._to_finding(first, "probe0001").model_dump())
        if missing:
            print(
                "\nThe list endpoint does not use the webhook payload's names for the fields "
                "above. _to_finding() degrades to defaults for those — correct it to the names "
                "actually present.",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
