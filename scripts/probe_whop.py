#!/usr/bin/env python3
"""One real call to Whop. Prints the raw response.

    make probe SVC=whop

CLAUDE.md rule 2: verify before you wire. The request and response *shapes* in
`app/clients/whop.py` are now verified against the Stainless types generated from Whop's OpenAPI
spec, so what is left for a live call to establish is narrower but not smaller:

  1. that the key works and the account can create a checkout at all,
  2. that the response really carries `purchase_url` and a `ch_…` id,
  3. that `metadata` is accepted and echoed.

The one thing this script **cannot** prove is the passthrough that our join key depends on:
`metadata.order_id` reappearing on the `payment.succeeded` webhook. That needs a completed
sandbox payment. The script prints the metadata it sent so you can diff it against what lands at
`/hooks/whop`.

This calls `WhopClient.create_checkout` rather than re-implementing the request, so a green probe
is evidence about the code that ships and not about a copy of it.
"""

from __future__ import annotations

import asyncio
import json
import sys

from app.clients.whop import (
    WHOP_API_BASE,
    WHOP_SANDBOX_API_BASE,
    WhopClient,
)
from app.config import settings

PROBE_ORDER_ID = "probe_order_0001"


def show(label: str, value: object) -> None:
    print(f"\n── {label} " + "─" * max(0, 76 - len(label)))
    if isinstance(value, dict | list):
        print(json.dumps(value, indent=2, default=str)[:4000])
    else:
        print(value)


async def main() -> int:
    if not settings.whop_api_key:
        print("WHOP_API_KEY is not set. See .env.example.", file=sys.stderr)
        return 2

    sandbox = "--sandbox" in sys.argv
    base_url = WHOP_SANDBOX_API_BASE if sandbox else WHOP_API_BASE

    show(
        "configuration under test",
        {
            "base_url": base_url,
            "environment": "sandbox" if sandbox else "production",
            "company_id": settings.whop_company_id,
            "plan_id": settings.whop_plan_id,
            "plan_path": "plan_id (existing)" if settings.whop_plan_id else "plan (inline)",
            "price_usd": settings.scan_price_usd,
        },
    )

    try:
        async with WhopClient(base_url=base_url) as whop:
            result = await whop.create_checkout(order_id=PROBE_ORDER_ID, url="https://example.com")
    except Exception as exc:
        show("failed", f"{type(exc).__name__}: {exc}")
        print(
            "\nRead the error above and correct the field it names. Do not guess a second "
            "field name — the authoritative shapes are the Stainless types under "
            ".venv/…/whop_sdk/types/checkout_configuration_create_params.py.",
            file=sys.stderr,
        )
        return 1

    raw = result.pop("raw", {})
    show("raw response", raw)
    show("what create_checkout returned", result)

    problems = []
    if not result.get("checkout_url"):
        problems.append("no checkout_url — neither purchase_url nor a plan id came back")
    if not result.get("checkout_id"):
        problems.append("no checkout_id — the payment.succeeded fallback join key will not work")
    if isinstance(raw, dict) and not raw.get("purchase_url"):
        problems.append("purchase_url absent; the URL above is the reconstructed fallback")

    if problems:
        show("problems", problems)
        return 1

    print(
        f"\nOK. Next: open {result['checkout_url']} and pay in sandbox, then confirm the "
        f"delivered payment.succeeded body carries metadata.order_id == '{PROBE_ORDER_ID}'. "
        "That passthrough is how a payment finds its scan, and nothing else verifies it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
