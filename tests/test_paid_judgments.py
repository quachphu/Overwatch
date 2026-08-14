"""Tests for the paths where a paid human judgment can be lost or forged.

Two thirds of the rubric is the human-in-the-loop measurement, and every judgment costs real
money, so these are the highest-value invariants in the codebase:

  * A judgment we cannot store must never be answered with a thank-you page.
  * One malformed field must not discard the good answers submitted alongside it.
  * A vote must be attributable to an assignment, or it is ballot stuffing.
  * A vote must never be recorded for the report the participant did not choose.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import pipeline
from app.db import session_scope
from app.main import app
from app.models import Assignment, Finding, Label, Preference, Report, new_id

PARTICIPANT = "prt_test_0001"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def scan_with_assignment():
    """A scan with one round-1 assignment covering two findings."""
    scan_id = pipeline.create_scan("https://93.184.216.34/shop")
    with session_scope() as session:
        ids = []
        for n in range(2):
            finding = Finding(
                id=new_id("fnd"),
                scan_id=scan_id,
                journey=f"journey {n}",
                step_intent="click",
                expected="works",
                observed="broken",
                screenshot_before_url="https://example.com/a.png",
                screenshot_after_url="https://example.com/b.png",
                source="seed",
                agent_severity="major",
                agent_confidence=0.5,
                category="server_error",
            )
            session.add(finding)
            session.flush()
            ids.append(finding.id)
        session.add(
            Assignment(
                scan_id=scan_id, round_no=1, slot=0, finding_ids=ids, participant_id=PARTICIPANT
            )
        )
    return scan_id, ids


class TestRound1SubmissionsAreNeverSilentlyDropped:
    def test_submission_without_an_assignment_is_refused(self, client):
        """The loop over `assignment.finding_ids` used to be skipped entirely for a missing
        assignment, so `saved` stayed 0 and we still rendered the thank-you page — telling
        someone we paid that their answers mattered while storing nothing."""
        response = client.post(
            "/t/r1/scn_does_not_exist",
            data={"participant_id": PARTICIPANT, "is_real_f1": "clear_yes", "severity_f1": "3"},
        )
        assert response.status_code == 409
        assert "could not record" in response.text.lower()
        assert "thank you" not in response.text.lower()

    def test_valid_labels_are_stored(self, client, scan_with_assignment):
        scan_id, ids = scan_with_assignment
        response = client.post(
            f"/t/r1/{scan_id}",
            data={
                "participant_id": PARTICIPANT,
                f"is_real_{ids[0]}": "clear_yes",
                f"severity_{ids[0]}": "4",
                f"is_real_{ids[1]}": "no",
                f"severity_{ids[1]}": "1",
            },
        )
        assert response.status_code == 200
        with session_scope() as session:
            rows = session.scalars(select(Label).where(Label.scan_id == scan_id)).all()
        assert len(rows) == 2

    def test_one_malformed_field_does_not_discard_the_good_labels(
        self, client, scan_with_assignment
    ):
        """`int(severity)` raised inside the session scope, which rolls back on any exception,
        so a single malformed field discarded every good label in the same submission."""
        scan_id, ids = scan_with_assignment
        response = client.post(
            f"/t/r1/{scan_id}",
            data={
                "participant_id": PARTICIPANT,
                f"is_real_{ids[0]}": "clear_yes",
                f"severity_{ids[0]}": "not-a-number",
                f"is_real_{ids[1]}": "probably",
                f"severity_{ids[1]}": "2",
            },
        )
        assert response.status_code == 200
        with session_scope() as session:
            rows = session.scalars(select(Label).where(Label.scan_id == scan_id)).all()
        assert [r.finding_id for r in rows] == [ids[1]], "the good label survived"
        assert "did not reach us intact" in response.text

    @pytest.mark.parametrize("severity", ["0", "6", "-1", "99"])
    def test_out_of_range_severity_is_rejected(self, client, scan_with_assignment, severity):
        scan_id, ids = scan_with_assignment
        client.post(
            f"/t/r1/{scan_id}",
            data={
                "participant_id": PARTICIPANT,
                f"is_real_{ids[0]}": "clear_yes",
                f"severity_{ids[0]}": severity,
            },
        )
        with session_scope() as session:
            rows = session.scalars(select(Label).where(Label.scan_id == scan_id)).all()
        assert rows == []

    def test_is_real_outside_the_vocabulary_is_rejected(self, client, scan_with_assignment):
        """An unrecognised verdict would be stored and then read as "not confirmed" by
        CONFIRMING_ANSWERS, letting a mangled POST silently suppress a finding."""
        scan_id, ids = scan_with_assignment
        client.post(
            f"/t/r1/{scan_id}",
            data={
                "participant_id": PARTICIPANT,
                f"is_real_{ids[0]}": "definitely-maybe",
                f"severity_{ids[0]}": "3",
            },
        )
        with session_scope() as session:
            rows = session.scalars(select(Label).where(Label.scan_id == scan_id)).all()
        assert rows == []


@pytest.fixture
def scan_ready_for_round2():
    scan_id = pipeline.create_scan("https://93.184.216.34/shop")
    with session_scope() as session:
        session.add(Report(scan_id=scan_id, version=1, ranked_finding_ids=["f1", "f2"]))
        session.add(Report(scan_id=scan_id, version=2, ranked_finding_ids=["f2", "f1"]))
        session.add(
            Assignment(
                scan_id=scan_id,
                round_no=2,
                slot=0,
                finding_ids=[],
                left_version=2,
                participant_id=PARTICIPANT,
            )
        )
    return scan_id


class TestRound2VotesCannotBeForgedOrInverted:
    def test_vote_without_an_assignment_is_refused(self, client):
        """The Preference was written regardless of the assignment, so anyone holding a scan id
        — and every participant has one, it is in their task URL — could POST unlimited votes
        and move `preference_share_v2`, the headline result."""
        response = client.post(
            "/t/r2/scn_does_not_exist",
            data={"participant_id": "prt_stuffer", "choice": "left"},
        )
        assert response.status_code == 409
        with session_scope() as session:
            rows = session.scalars(
                select(Preference).where(Preference.scan_id == "scn_does_not_exist")
            ).all()
        assert rows == []

    def test_side_is_taken_from_the_assignment_not_the_post(self, client, scan_ready_for_round2):
        """The assignment puts v2 on the left, so choosing "left" is a vote for v2."""
        scan_id = scan_ready_for_round2
        response = client.post(
            f"/t/r2/{scan_id}",
            data={"participant_id": PARTICIPANT, "choice": "left", "left_version": "2"},
        )
        assert response.status_code == 200
        with session_scope() as session:
            row = session.scalars(select(Preference).where(Preference.scan_id == scan_id)).first()
        assert row is not None
        assert row.chose_version == 2

    def test_a_disagreeing_side_is_refused_rather_than_inverted(
        self, client, scan_ready_for_round2
    ):
        """If the rendered page and the stored assignment disagree about which report was on the
        left, recording the vote would invert it — a preference for v1 from someone who picked
        v2. Refusing is the only safe option."""
        scan_id = scan_ready_for_round2
        response = client.post(
            f"/t/r2/{scan_id}",
            data={"participant_id": PARTICIPANT, "choice": "left", "left_version": "1"},
        )
        assert response.status_code == 409
        with session_scope() as session:
            rows = session.scalars(select(Preference).where(Preference.scan_id == scan_id)).all()
        assert rows == []

    def test_double_submit_records_one_vote(self, client, scan_ready_for_round2):
        scan_id = scan_ready_for_round2
        payload = {"participant_id": PARTICIPANT, "choice": "right", "left_version": "2"}
        client.post(f"/t/r2/{scan_id}", data=payload)
        client.post(f"/t/r2/{scan_id}", data=payload)
        with session_scope() as session:
            rows = session.scalars(select(Preference).where(Preference.scan_id == scan_id)).all()
        assert len(rows) == 1
