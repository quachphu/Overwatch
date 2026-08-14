"""Tests for the guards that stand between an agent and a real charge.

Every round launch hires people for money. The callers above `launch_round*` can all retry — the
Render task carries `max_retries=2`, the Recruiter agent can be re-prompted, and the operator
endpoint is a POST someone can double-click — and the participant count can arrive from an LLM
tool call. These tests assert the arithmetic and the idempotency, not the network.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import pipeline
from app.db import session_scope
from app.models import Finding, Round, new_id


def _finding(scan_id: str, *, evidence: bool = True) -> str:
    fid = new_id("fnd")
    with session_scope() as session:
        session.add(
            Finding(
                id=fid,
                scan_id=scan_id,
                journey="j",
                step_intent="click",
                expected="works",
                observed="broken",
                screenshot_before_url="https://example.com/a.png" if evidence else "",
                screenshot_after_url="https://example.com/b.png" if evidence else "",
                source="seed",
                agent_severity="major",
                agent_confidence=0.5,
                category="server_error",
            )
        )
    return fid


@pytest.fixture
def scan_id():
    return pipeline.create_scan("https://93.184.216.34/shop")


class TestLaunchIsIdempotent:
    """`_launched_round` is what makes a retry return the existing panel instead of hiring a
    second one. Keyed on a launched opportunity id rather than the existence of a Round row, so
    a run that died between inserting the row and creating the opportunity can still retry."""

    def test_no_round_yet(self, scan_id):
        assert pipeline._launched_round(scan_id, 1) is None

    def test_a_row_without_an_opportunity_does_not_block_a_retry(self, scan_id):
        with session_scope() as session:
            session.add(Round(scan_id=scan_id, round_no=1, project_id="prj_1", num_participants=12))
        assert pipeline._launched_round(scan_id, 1) is None

    def test_a_launched_round_is_returned(self, scan_id):
        with session_scope() as session:
            session.add(
                Round(
                    scan_id=scan_id,
                    round_no=1,
                    project_id="prj_1",
                    opportunity_id="opp_abc",
                    num_participants=12,
                )
            )
        existing = pipeline._launched_round(scan_id, 1)
        assert existing is not None
        assert existing["opportunity_id"] == "opp_abc"
        assert existing["already_launched"] is True

    def test_rounds_are_tracked_separately(self, scan_id):
        with session_scope() as session:
            session.add(
                Round(
                    scan_id=scan_id,
                    round_no=1,
                    project_id="prj_1",
                    opportunity_id="opp_r1",
                    num_participants=12,
                )
            )
        assert pipeline._launched_round(scan_id, 1) is not None
        assert pipeline._launched_round(scan_id, 2) is None, "round 2 must still be launchable"


class TestParticipantCountIsBounded:
    """This number multiplies real money and can originate in an LLM tool call."""

    def test_a_huge_request_is_clamped(self, scan_id):
        _finding(scan_id)
        plan = pipeline.prepare_round1(scan_id, num_participants=10_000)
        assert plan["num_participants"] == pipeline.MAX_PARTICIPANTS_PER_ROUND

    @pytest.mark.parametrize("count", [0, -5])
    def test_a_non_positive_request_is_refused_not_coerced(self, scan_id, count):
        """`requested or default` treated 0 as absent, so asking for zero participants hired the
        full default panel of twelve."""
        _finding(scan_id)
        with pytest.raises(ValueError, match="Refusing to guess"):
            pipeline.prepare_round1(scan_id, num_participants=count)

    def test_none_still_means_the_configured_default(self, scan_id):
        _finding(scan_id)
        plan = pipeline.prepare_round1(scan_id, num_participants=None)
        assert plan["num_participants"] == 12

    def test_the_planned_panel_is_not_clamped(self, scan_id):
        _finding(scan_id)
        plan = pipeline.prepare_round1(scan_id, num_participants=12)
        assert plan["num_participants"] == 12


class TestEvidenceIsRequiredBeforePaying:
    """A finding with no screenshots cannot be adjudicated: the task page promises two
    screenshots and renders none, so the only honest answer left is "Can't tell from this
    evidence" — which CONFIRMING_ANSWERS excludes, so every category falls below the 30%
    threshold and v2 is recalibrated on an artifact of missing evidence.

    `is_public_base()` cannot catch this. It inspects the base URL string, not whether any file
    exists, which is why ReplayQASource's screenshot-less findings sailed through it.
    """

    def test_findings_without_evidence_are_refused(self, scan_id):
        _finding(scan_id, evidence=False)
        _finding(scan_id, evidence=False)
        with pytest.raises(RuntimeError, match="none carry a screenshot"):
            pipeline.prepare_round1(scan_id)

    def test_findings_without_evidence_are_excluded_from_a_mixed_set(self, scan_id):
        good = _finding(scan_id, evidence=True)
        _finding(scan_id, evidence=False)
        plan = pipeline.prepare_round1(scan_id)
        assert plan["selected_finding_ids"] == [good]

    def test_a_scan_with_no_findings_at_all_still_says_so(self, scan_id):
        with pytest.raises(ValueError, match="no findings"):
            pipeline.prepare_round1(scan_id)


class TestVetoMustGateSomething:
    def test_a_veto_on_an_unknown_scan_is_refused(self):
        """Stored happily before, and gated nothing: `open_vetoes` for the real scan stayed
        empty, `release_report` released, and Critic had every reason to believe it blocked."""
        with pytest.raises(ValueError, match="unknown scan"):
            pipeline.record_veto("scn_typo", "PII in evidence")

    def test_a_veto_on_an_unknown_finding_is_refused(self, scan_id):
        with pytest.raises(ValueError, match="unknown finding"):
            pipeline.record_veto(scan_id, "PII", finding_id="fnd_nope")

    def test_a_valid_veto_blocks_release(self, scan_id):
        fid = _finding(scan_id)
        pipeline.record_veto(scan_id, "PII in evidence", finding_id=fid)
        assert pipeline.release_report(scan_id)["released"] is False

    def test_the_veto_is_attached_to_the_scan_it_names(self, scan_id):
        pipeline.record_veto(scan_id, "confirmation rate 0.21")
        assert len(pipeline.open_vetoes(scan_id)) == 1


class TestRoundRowsAreNotOrphaned:
    def test_launched_round_lookup_ignores_other_scans(self, scan_id):
        other = pipeline.create_scan("https://93.184.216.34/other")
        with session_scope() as session:
            session.add(
                Round(
                    scan_id=other,
                    round_no=1,
                    project_id="prj_1",
                    opportunity_id="opp_other",
                    num_participants=12,
                )
            )
        assert pipeline._launched_round(scan_id, 1) is None
        with session_scope() as session:
            rows = session.scalars(select(Round).where(Round.scan_id == other)).all()
        assert len(rows) == 1
