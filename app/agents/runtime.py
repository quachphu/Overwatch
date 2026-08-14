"""Shared Band wiring for all five agents.

Verified against the installed `band-sdk` on 2026-08-14 by introspection, not from memory:

    band.Agent.create(adapter, agent_id, api_key, ws_url=…, rest_url=…, config=…, …)
    band.AdapterFeatures(*, capabilities=(), emit=(), include_tools=None, exclude_tools=None,
                         include_categories=None)
    band.Emit -> EXECUTION | THOUGHTS | TASK_EVENTS | USAGE
    band.adapters.LangGraphAdapter(llm=None, checkpointer=None, graph_factory=None, graph=None,
                                   prompt_template='default', custom_section='',
                                   additional_tools=None, enable_memory_tools=False,
                                   enable_execution_reporting=False, …, features=None)
    band.config.load_agent_config(agent_key, *, config_path=None) -> tuple[agent_id, api_key]

One correction to the snippet in docs/AGENTS.md, which is otherwise accurate: it passes our
domain tools nowhere. `additional_tools=` is the parameter that puts them on the graph
alongside the free platform tools, so an agent whose adapter is built without it can chat about
scanning but cannot scan.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from app.agents.prompts import DISPLAY_NAMES, system_prompt
from app.agents.tools import toolset
from app.config import settings

logger = logging.getLogger("overwatch.agents")

CONFIG_PATH = Path(__file__).resolve().parents[2] / "agent_config.yaml"


def _llm():
    """The chat model, chosen by provider rather than hardcoded.

    docs/AGENTS.md wires `ChatOpenAI(model="gpt-5.5")`. `OVERWATCH_LLM_MODEL` overrides it so a
    model that turns out to be unavailable at the booth is one env var, not a code change.
    """
    from langchain_openai import ChatOpenAI

    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is unset. The Band agents need a chat model to run.")
    return ChatOpenAI(model=settings.llm_model, api_key=settings.openai_api_key)


def build_agent(agent_key: str):
    """Construct one registered Band agent. Does not connect."""
    from band import AdapterFeatures, Agent, Emit
    from band.adapters import LangGraphAdapter
    from band.config import load_agent_config
    from langgraph.checkpoint.memory import InMemorySaver

    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"{CONFIG_PATH.name} not found at {CONFIG_PATH}. Register five agents at "
            "app.band.ai/agents and write their ids and keys there — the key is shown once. "
            "See docs/AGENTS.md."
        )

    agent_id, api_key = load_agent_config(agent_key, config_path=CONFIG_PATH)

    adapter = LangGraphAdapter(
        llm=_llm(),
        checkpointer=InMemorySaver(),
        custom_section=system_prompt(agent_key),
        additional_tools=toolset(agent_key),
        # Emit.EXECUTION is not optional. Without it the room holds chat and nothing else —
        # no tool calls, no reasoning trail — and the audit trail the Band track is judged on
        # stays in a terminal nobody opens (docs/AGENTS.md, Wiring).
        features=AdapterFeatures(emit={Emit.EXECUTION}),
    )

    return Agent.create(adapter=adapter, agent_id=agent_id, api_key=api_key)


async def run_agent(agent_key: str) -> None:
    """Connect one agent and serve until interrupted.

    One process per agent, always. A second process on the same `agent_id` silently drops the
    first with no error on either side (CLAUDE.md, Band gotchas) — which presents as an agent
    that mysteriously stops answering, and costs an hour to diagnose if you have not seen it
    before.
    """
    agent = build_agent(agent_key)
    logger.info(
        "%s connecting to Band (model %s, tools: %s)",
        DISPLAY_NAMES[agent_key],
        settings.llm_model,
        ", ".join(t.name for t in toolset(agent_key)),
    )
    await agent.run()


def main(argv: list[str] | None = None) -> int:
    """`python -m app.agents.runtime <agent_key>`, and the entrypoint each module reuses."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    args = argv if argv is not None else sys.argv[1:]
    agent_key = (args[0] if args else os.environ.get("OVERWATCH_AGENT", "")).lower()
    if agent_key not in DISPLAY_NAMES:
        print(
            f"usage: python -m app.agents.runtime <{'|'.join(DISPLAY_NAMES)}>",
            file=sys.stderr,
        )
        return 2

    try:
        asyncio.run(run_agent(agent_key))
    except KeyboardInterrupt:
        logger.info("%s shutting down.", DISPLAY_NAMES[agent_key])
    except RuntimeError as exc:
        # Missing config or missing key: a one-line reason beats a traceback when five panes
        # are starting at once and one of them fails.
        print(f"{DISPLAY_NAMES[agent_key]}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
