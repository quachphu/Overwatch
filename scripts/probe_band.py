#!/usr/bin/env python3
"""Pre-flight for the Band crew. Builds all five agents without connecting any of them.

Worth having because every Band failure mode we know about is silent:

  * A second process on the same `agent_id` drops the first with **no error on either side**,
    so a duplicated id in `agent_config.yaml` presents as one agent that mysteriously never
    answers. This checks that all five ids are distinct before you spend time debugging that.
  * Without `Emit.EXECUTION` the room holds chat only — no tool calls, no reasoning — and the
    audit trail the Band track is judged on quietly does not exist.
  * An adapter built without `additional_tools` produces an agent that talks about scanning
    and cannot scan.

Run this before `make agents`:

    make probe SVC=band
"""

from __future__ import annotations

import sys

from app.agents.prompts import DISPLAY_NAMES, PROMPTS, system_prompt
from app.agents.runtime import CONFIG_PATH
from app.agents.tools import toolset

EXPECTED_TOOL_OWNERSHIP = {
    # docs/AGENTS.md, Recruiter: "You are the only agent that talks to Terac."
    "launch_verification_round": {"recruiter"},
    "launch_comparison_round": {"recruiter"},
    # A veto is terminal until Critic lifts it, so only Critic holds the lift.
    "lift_block": {"critic"},
    "block_release": {"critic"},
}


def main() -> int:
    ok = True

    print("── config " + "─" * 68)
    if not CONFIG_PATH.exists():
        print(f"MISSING {CONFIG_PATH}")
        print("  Copy agent_config.yaml.example and fill in five registrations.")
        return 2
    print(f"found {CONFIG_PATH}")

    from band.config import load_agent_config

    print("\n── registrations " + "─" * 61)
    seen: dict[str, str] = {}
    for key in PROMPTS:
        try:
            agent_id, api_key = load_agent_config(key, config_path=CONFIG_PATH)
        except Exception as exc:
            print(f"{DISPLAY_NAMES[key]:<16} FAILED  {type(exc).__name__}: {exc}")
            ok = False
            continue

        if not agent_id or not api_key:
            print(f"{DISPLAY_NAMES[key]:<16} EMPTY   agent_id or api_key is blank")
            ok = False
            continue

        if agent_id in seen:
            # The silent one. Worth failing loudly here instead.
            print(
                f"{DISPLAY_NAMES[key]:<16} DUPLICATE agent_id shared with "
                f"{DISPLAY_NAMES[seen[agent_id]]} — the first connection would be dropped "
                "with no error."
            )
            ok = False
        else:
            seen[agent_id] = key
            print(f"{DISPLAY_NAMES[key]:<16} ok      {agent_id[:8]}… key {len(api_key)} chars")

    print("\n── toolsets " + "─" * 66)
    owners: dict[str, set[str]] = {}
    for key in PROMPTS:
        tools = toolset(key)
        for tool in tools:
            owners.setdefault(tool.name, set()).add(key)
        print(f"{DISPLAY_NAMES[key]:<16} {', '.join(t.name for t in tools)}")

    print("\n── least privilege " + "─" * 59)
    for tool_name, expected in EXPECTED_TOOL_OWNERSHIP.items():
        actual = owners.get(tool_name, set())
        if actual == expected:
            print(f"{tool_name:<28} ok      only {', '.join(sorted(actual))}")
        else:
            print(f"{tool_name:<28} FAILED  expected {sorted(expected)}, got {sorted(actual)}")
            ok = False

    print("\n── prompts " + "─" * 67)
    for key in PROMPTS:
        prompt = system_prompt(key)
        # The preamble must lead: the routing and budget rules govern the role block.
        assert prompt.startswith("You are one of five agents"), key
        print(f"{DISPLAY_NAMES[key]:<16} {len(prompt)} chars, preamble + role")

    print("\n── adapters " + "─" * 66)
    from app.agents.runtime import build_agent

    for key in PROMPTS:
        try:
            build_agent(key)
        except Exception as exc:
            print(f"{DISPLAY_NAMES[key]:<16} FAILED  {type(exc).__name__}: {exc}")
            ok = False
            continue
        print(f"{DISPLAY_NAMES[key]:<16} built (Emit.EXECUTION on, tools attached)")

    print()
    if ok:
        print("All five agents build. `make agents` will connect them, one process each.")
        return 0
    print("Fix the failures above before running make agents.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
