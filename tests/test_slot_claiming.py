"""Concurrent participants must not collide on an assignment slot.

Terac participants arrive in a burst the instant an opportunity goes live, so this is the normal
case rather than an exotic one.

What a collision costs: `triage.assign_round1` guarantees each finding gets the same number of
raters give or take one, and "confirmed = strict majority of 3" depends on that. If two people
share a slot, their findings collect six raters while another slot's findings collect none — and
`summarize_labels` then reports a majority verdict for some findings and nothing for others,
after we have already paid for every judgment.

These tests run real threads against the real database rather than mocking the session, because
the bug being guarded is in the interleaving of SQL statements. A mock would reproduce whatever
interleaving the test author imagined.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import delete, select

from app.db import session_scope
from app.main import _claim_slot
from app.models import Assignment

ROUND = 1
N_SLOTS = 12


@pytest.fixture
def scan_with_slots(request):
    """A scan id with `N_SLOTS` unclaimed assignment slots."""
    scan_id = f"scan_slots_{request.node.name[:24]}"
    with session_scope() as session:
        session.execute(delete(Assignment).where(Assignment.scan_id == scan_id))
        for slot in range(N_SLOTS):
            session.add(
                Assignment(
                    scan_id=scan_id,
                    round_no=ROUND,
                    slot=slot,
                    finding_ids=[f"f{slot}a", f"f{slot}b"],
                    left_version=1 + (slot % 2),
                )
            )
    yield scan_id
    with session_scope() as session:
        session.execute(delete(Assignment).where(Assignment.scan_id == scan_id))


def _claim_concurrently(scan_id: str, participant_ids: list[str]) -> dict[str, int | None]:
    """Fire every claim from its own thread, released together."""
    results: dict[str, int | None] = {}
    lock = threading.Lock()
    start = threading.Barrier(len(participant_ids))

    def worker(pid: str) -> None:
        start.wait()
        assignment = _claim_slot(scan_id, ROUND, pid)
        with lock:
            results[pid] = None if assignment is None else assignment.slot

    threads = [threading.Thread(target=worker, args=(pid,)) for pid in participant_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


class TestConcurrentDistinctParticipants:
    def test_no_two_participants_share_a_slot(self, scan_with_slots):
        participants = [f"p{i}" for i in range(N_SLOTS)]
        results = _claim_concurrently(scan_with_slots, participants)

        slots = [slot for slot in results.values() if slot is not None]
        assert len(slots) == len(
            set(slots)
        ), f"Two participants received the same slot: {sorted(results.items())}"

    def test_every_slot_is_used_when_participants_match_slots(self, scan_with_slots):
        """A lost slot is a slot we paid for and got nothing from."""
        participants = [f"p{i}" for i in range(N_SLOTS)]
        results = _claim_concurrently(scan_with_slots, participants)

        assert sorted(s for s in results.values() if s is not None) == list(range(N_SLOTS))

    def test_database_agrees_with_what_callers_were_told(self, scan_with_slots):
        participants = [f"p{i}" for i in range(N_SLOTS)]
        results = _claim_concurrently(scan_with_slots, participants)

        with session_scope() as session:
            rows = session.scalars(
                select(Assignment).where(Assignment.scan_id == scan_with_slots)
            ).all()
            persisted = {row.participant_id: row.slot for row in rows if row.participant_id}

        assert persisted == {p: s for p, s in results.items() if s is not None}

    def test_surplus_participants_are_turned_away_not_doubled_up(self, scan_with_slots):
        """More arrivals than slots must yield `None`, never a shared slot.

        `None` renders the "this task is full" page, which is recoverable. A shared slot is not.
        """
        participants = [f"p{i}" for i in range(N_SLOTS + 6)]
        results = _claim_concurrently(scan_with_slots, participants)

        slots = [slot for slot in results.values() if slot is not None]
        assert len(slots) == N_SLOTS
        assert len(set(slots)) == N_SLOTS
        assert sum(1 for slot in results.values() if slot is None) == 6


class TestSameParticipantTwice:
    def test_refresh_returns_the_same_slot(self, scan_with_slots):
        """Sequential re-entry: the stable-assignment property from SPECS.md §5.3."""
        first = _claim_slot(scan_with_slots, ROUND, "p_refresh")
        second = _claim_slot(scan_with_slots, ROUND, "p_refresh")
        assert first is not None and second is not None
        assert first.slot == second.slot
        assert first.left_version == second.left_version

    def test_double_tap_does_not_consume_two_slots(self, scan_with_slots):
        """A phone user double-tapping the link fires two near-simultaneous requests."""
        results = _claim_concurrently(scan_with_slots, ["p_same"] * 1)  # warm the path
        assert results

        # Now genuinely concurrent, same participant id, via distinct threads.
        got: list[int | None] = []
        lock = threading.Lock()
        start = threading.Barrier(4)

        def worker() -> None:
            start.wait()
            assignment = _claim_slot(scan_with_slots, ROUND, "p_double")
            with lock:
                got.append(None if assignment is None else assignment.slot)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        with session_scope() as session:
            held = session.scalars(
                select(Assignment).where(
                    Assignment.scan_id == scan_with_slots,
                    Assignment.participant_id == "p_double",
                )
            ).all()

        assert len(held) == 1, f"one participant holds {len(held)} slots"
        assert set(got) == {held[0].slot}


class TestNoSlots:
    def test_returns_none_rather_than_raising(self):
        assert _claim_slot("scan_does_not_exist", ROUND, "p1") is None
