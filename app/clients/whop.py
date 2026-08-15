"""Whop payments client.

Revenue is **second in the cut order** (docs/RUNBOOK.md), so this file is deliberately thin and
every failure path degrades to "scan anyway, unpaid" rather than blocking the pipeline.

Written against `httpx` rather than `whop_sdk` for one reason: the two facts we depend on — the
checkout URL and the `metadata` passthrough — must be visible and correctable in one line, and
an SDK hides both behind method calls.

**Verification status: request and response shapes verified against the live OpenAPI spec**
(`https://docs.whop.com/api-reference/beta/checkout-configurations/create-a-checkout-configuration`,
fetched 2026-08-15, `x-api-version-date: 2026-08-13`) — not just the installed `whop_sdk` 0.0.41
package. That package's `checkout_configuration_create_params.py` names the top-level and nested
plan account field **`company_id`**; the live spec names it **`account_id`** in every one of its
five occurrences, and shows `"account_id is required"` as an example 400 body. The SDK's generated
types are stale relative to the API that is actually live. Sending the wrong key does not 400 by
itself — an unrecognized field is silently dropped — but it means Whop can never resolve which
account owns the inline plan, which is the likely reason a real request came back rejecting an
unrelated nested field (`plan.unlimited_stock`, which *is* present in the payload) rather than
naming the field that was actually missing. See RESEARCH.md §13.11.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# whop_sdk/_client.py: `base_url = f"https://api.whop.com/api/v1"`.
WHOP_API_BASE = "https://api.whop.com/api/v1"
# docs: https://docs.whop.com/developer/guides/sandbox — sandbox and production are separate
# environments with separate keys. Real hackathon revenue requires production.
WHOP_SANDBOX_API_BASE = "https://sandbox-api.whop.com/api/v1"
CHECKOUT_ENDPOINT = "/checkout_configurations"

# Fallback only. The create response returns `purchase_url` ("Checkout URL you can send to
# customers"), which is authoritative and used in preference to this template.
CHECKOUT_URL_TEMPLATE = "https://whop.com/checkout/{plan_id}"

PAYMENT_SUCCEEDED_EVENT = "payment.succeeded"


class WhopClient:
    def __init__(
        self,
        api_key: str | None = None,
        company_id: str | None = None,
        *,
        base_url: str = WHOP_API_BASE,
    ) -> None:
        self.api_key = api_key or settings.whop_api_key
        self.company_id = company_id or settings.whop_company_id
        self.base_url = base_url
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> WhopClient:
        if not self.api_key:
            raise RuntimeError("WHOP_API_KEY is not set. See .env.example.")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=20.0,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def create_checkout(self, *, order_id: str, url: str) -> dict[str, Any]:
        """Create a checkout configuration and return a payable link.

        `metadata` is the join key: the spec describes it as "Custom key-value metadata copied
        to payments and memberships", which is what makes `metadata.order_id` reappear on the
        `payment.succeeded` webhook.

        Two ways to say what is being charged for, and the spec marks them **mutually
        exclusive** — sending both is a 400:

        * `plan_id` — an existing `plan_…`, used when WHOP_PLAN_ID is configured.
        * `plan` — inline attributes, where Whop will "create or find a plan". This is the path
          when no plan was pre-created in the dashboard, which is the likely hackathon state.
        """
        if self._client is None:
            raise RuntimeError("Use WhopClient as an async context manager.")

        payload: dict[str, Any] = {
            # "Checkout mode: `payment` collects payment for a plan now". Explicit rather than
            # relying on the documented default, so a default change cannot silently make our
            # checkout collect nothing.
            "mode": "payment",
            "metadata": {"order_id": order_id, "target_url": url},
        }
        if self.company_id:
            # Wire field is `account_id`, not `company_id` — see the module docstring. Set on
            # both the checkout configuration and the inline plan; the plan's copy is optional
            # and defaults to "the account resolved from the request", but an API key scoped to
            # more than one account has nothing to resolve from without the top-level one.
            payload["account_id"] = self.company_id

        if settings.whop_plan_id:
            payload["plan_id"] = settings.whop_plan_id
        else:
            payload["plan"] = {
                "plan_type": "one_time",
                "initial_price": float(settings.scan_price_usd),
                "currency": "usd",
                "release_method": "buy_now",
                "title": "Overwatch QA scan",
                "unlimited_stock": True,
                # Without this, a second scan reuses the first scan's plan. Harmless for
                # billing, but it keeps the dashboard readable.
                "force_create_new_plan": False,
            }
            if self.company_id:
                payload["plan"]["account_id"] = self.company_id

        response = await self._client.post(CHECKOUT_ENDPOINT, json=payload)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Whop {response.status_code} on POST {CHECKOUT_ENDPOINT}: "
                f"{response.text[:500]}"
            )

        body = response.json()
        plan_id = (body.get("plan") or {}).get("id") or settings.whop_plan_id
        checkout_url = body.get("purchase_url") or (
            CHECKOUT_URL_TEMPLATE.format(plan_id=plan_id) if plan_id else None
        )
        return {
            # "Checkout configuration ID, prefixed `ch_`." Stored on the Order as the fallback
            # join key for payment.succeeded.
            "checkout_id": body.get("id"),
            "plan_id": plan_id,
            "checkout_url": checkout_url,
            "raw": body,
        }
