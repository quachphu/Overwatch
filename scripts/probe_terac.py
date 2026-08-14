"""
probe_terac.py — THE FIRST THING WE RUN. Target: 10:45.

Purpose is not to build anything. It is to measure ONE number: how long Terac
actually takes to deliver completed submissions. The API docs say
expected_days_to_complete has a 5-day minimum; the event page promises results
"within hours". Those cannot both be true and the whole day's schedule depends
on which one is.

Run it, note the launch time, then go build. Check back with --watch.

    python scripts/probe_terac.py --launch
    python scripts/probe_terac.py --watch <opportunity_id>

Every field below is from the docs the agent fetched in Phase 0. If a request
4xxs, DO NOT guess at a fix — read the error body, find the field in the docs,
and correct it. The error format is {"error": {"code", "message", "details"[]}}.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

import httpx

BASE = os.environ.get("TERAC_BASE_URL", "https://terac.com/api/external/v2")
KEY = os.environ.get("TERAC_API_KEY")

if not KEY:
    sys.exit("TERAC_API_KEY not set. It comes in the attendee doc at 09:15.")

HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

# UNKNOWN #1: is the 5-day minimum waived for hackathon keys? Confirm at booth.
# If waived, lower this. It is a ceiling on patience, not a promise of slowness.
EXPECTED_DAYS = 5

# A public page that renders instantly and needs no interaction. Replace with
# your own deployed /t/smoke once the Ingress is up.
SMOKE_TASK_URL = (
    os.environ.get("PUBLIC_BASE_URL", "https://example.com") + "/t/smoke?pid={{participant_id}}"
)


def dump(label: str, r: httpx.Response) -> dict:
    """Print the raw response. Read the actual shape before integrating."""
    print(f"\n─── {label} · HTTP {r.status_code} ───")
    try:
        body = r.json()
        print(json.dumps(body, indent=2)[:3000])
        return body
    except Exception:
        print(r.text[:2000])
        return {}


def launch() -> None:
    with httpx.Client(headers=HEADERS, timeout=30) as c:
        # 1 — project
        r = c.post(f"{BASE}/projects", json={"name": "Overwatch smoke test"})
        proj = dump("create project", r)
        if r.status_code >= 400:
            sys.exit("project creation failed — read the error above, do not guess")
        project_id = proj.get("id")

        # 2 — draft opportunity.
        # NOTE: quotas require screening_questions or you get BAD_REQUEST.
        # NOTE: qualify_logic "must" is valid on pick:"one" AND pick:"any" — on single-select it
        # collapses with may/must_one_of, so only reject/review change the outcome there.
        payload = {
            "title": "Quick 2-minute screenshot check",
            "project_id": project_id,
            "num_participants": 5,
            "business_type": "b2c",
            "expected_days_to_complete": EXPECTED_DAYS,
            "filters": [
                {"multi_select--country": {"$in": ["US"]}},
                {"integer--age": {"$gte": 18, "$lte": 65}},
            ],
            "screening_questions": [
                {
                    "key": "attn",
                    "text": "What will you be looking at in this task?",
                    "pick": "one",
                    "answers": [
                        {"text": "Screenshots of a website", "qualify_logic": "must"},
                        {"text": "An audio recording", "qualify_logic": "reject"},
                    ],
                }
            ],
            "tasks": [
                {
                    "sequence": 1,
                    "task_type": "survey",
                    "review_type": "auto_approve",
                    "task_url": SMOKE_TASK_URL,
                    "duration_minutes": 2,
                }
            ],
        }
        r = c.post(f"{BASE}/opportunities", json=payload)
        opp = dump("create opportunity (draft)", r)
        if r.status_code >= 400:
            sys.exit("opportunity creation failed — read the error, find the field in the docs")
        opp_id = opp.get("id")

        # 3 — launch. Bodyless POST returns 415, hence the {}.
        r = c.post(f"{BASE}/opportunities/{opp_id}/launch", json={})
        if r.status_code == 409:
            print("409 — already active. Treating as success (idempotent).")
        else:
            dump("launch", r)

        print("\n" + "=" * 60)
        print(f"  LAUNCHED {opp_id}")
        print(f"  T0 = {datetime.now():%H:%M:%S}")
        print(f"  python scripts/probe_terac.py --watch {opp_id}")
        print("=" * 60)
        print("""
  DECISION TREE — this number sets the whole day:
    fills < 45 min   -> full two-round plan
    45-120 min       -> collapse round 2 into the tail of round 1
    nothing by 14:00 -> pivot the human task, and say so plainly in the pitch
""")


def watch(opp_id: str) -> None:
    t0 = time.time()
    with httpx.Client(headers=HEADERS, timeout=30) as c:
        while True:
            # Rate limit is 100/min. 20s is 3/min. Do not tighten this.
            r = c.get(
                f"{BASE}/opportunities/{opp_id}/submissions",
                params={"limit": 100},
            )
            if r.status_code >= 400:
                dump("poll error", r)
                return
            data = r.json().get("data", [])
            counts: dict[str, int] = {}
            for s in data:
                counts[s.get("status", "?")] = counts.get(s.get("status", "?"), 0) + 1
            mins = (time.time() - t0) / 60
            print(f"[{datetime.now():%H:%M:%S}] +{mins:5.1f}m  {counts or 'nothing yet'}")
            if counts.get("approved", 0) >= 1:
                print(f"\n>>> FIRST COMPLETION at +{mins:.1f} minutes. This is the number.\n")
            time.sleep(20)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--launch", action="store_true")
    p.add_argument("--watch", metavar="OPPORTUNITY_ID")
    a = p.parse_args()
    if a.launch:
        launch()
    elif a.watch:
        watch(a.watch)
    else:
        p.print_help()
