#!/usr/bin/env python3
"""End-to-end check of `/hooks/whop` against a running server.

    PYTHONPATH=. python scripts/e2e_whop_webhook.py --base http://localhost:8021 \
        --db /tmp/ow/e2e.db --secret whsec_e2e

`tests/test_security.py` covers `verify_whop_signature` in isolation. This covers the wiring
around it — that the route reads the three Standard Webhooks headers under their real names, that
a forged payment cannot start a paid scan, that a retry with the same `webhook-id` does not scan
twice, and that a verified payment actually reaches `pipeline.create_scan`.

Requires a server started with `BUG_SOURCE=seed` and `WHOP_WEBHOOK_SECRET` set to `--secret`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import sqlite3
import time
import urllib.error
import urllib.request


def post(base: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(
        base + "/hooks/whop", data=body, headers={"Content-Type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()[:120]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:120]


def sign(secret: str, wid: str, ts: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{wid}.{ts}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8021")
    ap.add_argument("--db", default="/tmp/ow/e2e.db")
    ap.add_argument("--secret", default="whsec_e2e")
    args = ap.parse_args()
    secret, base = args.secret, args.base

    order_id = "ord_e2etest01"
    con = sqlite3.connect(args.db)
    con.execute(
        "INSERT OR REPLACE INTO orders (id,url,status,amount_usd,created_at) "
        "VALUES (?,?,?,?,datetime('now'))",
        (order_id, "https://example.com", "pending", 49.0),
    )
    con.commit()
    con.close()

    payment = {
        "id": "evt_1",
        "type": "payment.succeeded",
        "data": {
            "id": "pay_1",
            # Both spellings present on purpose: the spec example and the guide disagree, and the
            # handler must ignore both in favour of the envelope `type`.
            "status": "paid",
            "substatus": "succeeded",
            "metadata": {"order_id": order_id},
        },
    }
    body = json.dumps(payment).encode()
    ts = str(int(time.time()))
    stale = str(int(time.time()) - 3600)
    other = json.dumps({"id": "evt_9", "type": "membership.went_valid", "data": {}}).encode()

    # (name, expected status, actual)
    checks = [
        (
            "valid signature",
            200,
            post(
                base,
                body,
                {
                    "webhook-id": "msg_1",
                    "webhook-timestamp": ts,
                    "webhook-signature": sign(secret, "msg_1", ts, body),
                },
            ),
        ),
        (
            "retry, same webhook-id",
            200,
            post(
                base,
                body,
                {
                    "webhook-id": "msg_1",
                    "webhook-timestamp": ts,
                    "webhook-signature": sign(secret, "msg_1", ts, body),
                },
            ),
        ),
        (
            "secret rotation, 2 sigs",
            200,
            post(
                base,
                body,
                {
                    "webhook-id": "msg_6",
                    "webhook-timestamp": ts,
                    "webhook-signature": sign("old_key", "msg_6", ts, body)
                    + " "
                    + sign(secret, "msg_6", ts, body),
                },
            ),
        ),
        (
            "non-payment event",
            200,
            post(
                base,
                other,
                {
                    "webhook-id": "msg_7",
                    "webhook-timestamp": ts,
                    "webhook-signature": sign(secret, "msg_7", ts, other),
                },
            ),
        ),
        (
            "wrong secret",
            401,
            post(
                base,
                body,
                {
                    "webhook-id": "msg_2",
                    "webhook-timestamp": ts,
                    "webhook-signature": sign("wrong", "msg_2", ts, body),
                },
            ),
        ),
        (
            "tampered order_id",
            401,
            post(
                base,
                body.replace(order_id.encode(), b"ord_VICTIM01"),
                {
                    "webhook-id": "msg_3",
                    "webhook-timestamp": ts,
                    "webhook-signature": sign(secret, "msg_3", ts, body),
                },
            ),
        ),
        (
            "stale timestamp",
            401,
            post(
                base,
                body,
                {
                    "webhook-id": "msg_4",
                    "webhook-timestamp": stale,
                    "webhook-signature": sign(secret, "msg_4", stale, body),
                },
            ),
        ),
        (
            "terac-style signature",
            401,
            post(
                base,
                body,
                {
                    "webhook-id": "msg_5",
                    "webhook-timestamp": ts,
                    "webhook-signature": base64.b64encode(
                        hmac.new(secret.encode(), ts.encode() + body, hashlib.sha256).digest()
                    ).decode(),
                },
            ),
        ),
        ("no headers", 401, post(base, body, {})),
    ]

    failures = 0
    print(f"{'case':26} {'want':>4} {'got':>4}")
    for name, want, (got, text) in checks:
        ok = got == want
        failures += not ok
        print(f"{name:26} {want:>4} {got:>4}  {'ok' if ok else 'FAIL'}  {text if not ok else ''}")

    # The scan runs in a BackgroundTask, so give it a moment before asserting on the DB.
    time.sleep(8)
    con = sqlite3.connect(args.db)
    status, scan_id = con.execute(
        "SELECT status, scan_id FROM orders WHERE id=?", (order_id,)
    ).fetchone()
    scans = con.execute("SELECT id, url, status FROM scans").fetchall()
    n_findings = con.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    events = con.execute("SELECT event_id, event_type FROM webhook_events").fetchall()
    con.close()

    print(f"\norder: status={status} scan_id={scan_id}")
    print(f"scans: {scans}")
    print(f"findings: {n_findings}")
    print(f"webhook_events stored: {events}")

    outcomes = [
        ("order marked paid", status == "paid"),
        ("scan created and joined", bool(scan_id)),
        ("exactly one scan (retry did not double-scan)", len(scans) == 1),
        ("findings produced", n_findings > 0),
        ("only verified events stored", {e[0] for e in events} == {"msg_1", "msg_6", "msg_7"}),
    ]
    print()
    for name, ok in outcomes:
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}")

    print("\n" + ("PASS" if not failures else f"{failures} FAILURE(S)"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
