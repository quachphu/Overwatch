"""Locks in the wire field name for the Whop checkout account association.

RESEARCH.md §13.11: `whop_sdk` 0.0.41's generated types name this field `company_id`, but the
live OpenAPI spec (fetched 2026-08-15) names it `account_id` in all five occurrences, both at
the top level and nested under `plan`. Sending the wrong name does not itself 400 — an
unrecognized field is silently dropped — which is exactly the kind of bug that stays invisible
until a real checkout is attempted, so it is pinned here rather than left to be caught by hand
a second time.
"""

import json

import httpx
import pytest

from app.clients.whop import CHECKOUT_ENDPOINT, WhopClient


def _client_with_transport(transport: httpx.MockTransport, **kwargs) -> WhopClient:
    client = WhopClient(api_key="apik_test", **kwargs)
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=transport,
        headers={"Authorization": "Bearer apik_test", "Content-Type": "application/json"},
    )
    return client


@pytest.mark.asyncio
async def test_checkout_sends_account_id_not_company_id():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "ch_test", "purchase_url": "https://whop.com/x"})

    client = _client_with_transport(httpx.MockTransport(handler), company_id="biz_test")
    await client.create_checkout(order_id="ord_1", url="https://example.com")

    assert captured["body"]["account_id"] == "biz_test"
    assert "company_id" not in captured["body"]
    assert captured["body"]["plan"]["account_id"] == "biz_test"


@pytest.mark.asyncio
async def test_checkout_omits_account_id_when_unset():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "ch_test", "purchase_url": "https://whop.com/x"})

    client = _client_with_transport(httpx.MockTransport(handler), company_id=None)
    # WhopClient.__init__ does `company_id or settings.whop_company_id`, so passing None only
    # means "fall back to whatever's in the environment" — it can't force "unset" if the
    # developer's own .env has a real WHOP_COMPANY_ID (as it should, outside this test). Set the
    # attribute directly to pin the behaviour this test actually cares about.
    client.company_id = ""
    await client.create_checkout(order_id="ord_1", url="https://example.com")

    assert "account_id" not in captured["body"]
    assert "account_id" not in captured["body"]["plan"]


@pytest.mark.asyncio
async def test_checkout_endpoint_path():
    assert CHECKOUT_ENDPOINT == "/checkout_configurations"
