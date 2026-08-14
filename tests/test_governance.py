"""Tests for the governance property.

docs/AGENTS.md makes two claims about the crew that only mean something if they hold when a
model decides otherwise:

  * A Critic veto is terminal until *Critic* lifts it, and Bursar never overrides it.
  * Recruiter is the only agent that talks to Terac.

Both are stated as instructions in prompts, and a prompt is a request. These tests assert they
are also properties of the code and of the tool graph — which is the difference between a
governance story and a governance feature.
"""

from __future__ import annotations

import pytest

from app import pipeline
from app.agents import tools


@pytest.fixture
def scan_id():
    return pipeline.create_scan("https://93.184.216.34/shop")


class TestVetoIsTerminal:
    def test_release_succeeds_when_nothing_is_blocking(self, scan_id):
        assert pipeline.release_report(scan_id)["released"] is True

    def test_release_is_refused_while_a_veto_is_open(self, scan_id):
        pipeline.record_veto(scan_id, "PII in finding: customer email in console output")
        result = pipeline.release_report(scan_id)
        assert result["released"] is False
        assert result["vetoes"]

    def test_bursars_tool_cannot_override_it(self, scan_id):
        """Bursar's `release` tool takes a scan id and nothing else.

        There is no `force` argument to find, so the model has no way to comply with a request
        to release a blocked report even if asked directly.
        """
        pipeline.record_veto(scan_id, "confirmation rate 0.21")
        payload = tools.release.invoke({"scan_id": scan_id})
        assert '"released": false' in payload

        release_args = set(tools.release.args_schema.model_fields)
        assert release_args == {"scan_id"}

    def test_release_works_again_once_critic_lifts_it(self, scan_id):
        veto_id = pipeline.record_veto(scan_id, "PII in finding find_x")
        assert pipeline.release_report(scan_id)["released"] is False

        tools.lift_block.invoke({"veto_id": veto_id})
        assert pipeline.open_vetoes(scan_id) == []
        assert pipeline.release_report(scan_id)["released"] is True

    def test_one_open_veto_blocks_even_when_another_is_cleared(self, scan_id):
        first = pipeline.record_veto(scan_id, "PII in finding find_a")
        pipeline.record_veto(scan_id, "overclaimed root cause on find_b")
        pipeline.clear_veto(first)
        assert len(pipeline.open_vetoes(scan_id)) == 1
        assert pipeline.release_report(scan_id)["released"] is False


class TestLeastPrivilege:
    """Ownership is asserted here rather than left to the prompts, because a shared tool list
    would silently erase the asymmetry that makes the crew worth having."""

    @staticmethod
    def owners(tool_name: str) -> set[str]:
        return {
            agent
            for agent, toolset in tools.TOOLSETS.items()
            if any(t.name == tool_name for t in toolset)
        }

    @pytest.mark.parametrize("tool_name", ["launch_verification_round", "launch_comparison_round"])
    def test_only_recruiter_talks_to_terac(self, tool_name):
        assert self.owners(tool_name) == {"recruiter"}

    @pytest.mark.parametrize("tool_name", ["block_release", "lift_block"])
    def test_only_critic_can_block_or_unblock(self, tool_name):
        assert self.owners(tool_name) == {"critic"}

    def test_only_bursar_releases(self):
        assert self.owners("release") == {"bursar"}

    def test_triage_cannot_spend_credit(self):
        triage_tools = {t.name for t in tools.toolset("triage")}
        assert "launch_verification_round" not in triage_tools
        assert "launch_comparison_round" not in triage_tools

    def test_every_agent_has_at_least_one_tool(self):
        """An agent with no tools can only talk, and the delete test would pass without it."""
        for agent, toolset in tools.TOOLSETS.items():
            assert toolset, agent


class TestRoundTwoIsBlindAndFresh:
    def test_prepare_round2_needs_both_report_versions(self, scan_id):
        with pytest.raises(ValueError, match="both report versions"):
            pipeline.prepare_round2(scan_id)

    def test_left_side_is_randomised_across_slots(self, scan_id):
        """If v2 were always on the left, position would carry the answer.

        A participant who can infer which column is the new report is expressing a guess about
        our process, not a preference between two reports.
        """
        pipeline.write_report(scan_id, version=1, ranked=["a", "b"])
        pipeline.write_report(scan_id, version=2, ranked=["b", "a"])
        prepared = pipeline.prepare_round2(scan_id, num_participants=35)

        assert prepared["left_v1_slots"] + prepared["left_v2_slots"] == 35
        assert prepared["left_v1_slots"] > 0
        assert prepared["left_v2_slots"] > 0
