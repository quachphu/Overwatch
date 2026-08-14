"""Tests for the Terac opportunity payloads.

These assert the constraints listed in RESEARCH.md §2 — each one is a documented way to earn a
4xx, and each 4xx costs us a round we may not have time to relaunch. They are cheap tests that
buy back the forty minutes a guessed field name costs at 16:00.

They validate *shape*, not the API's acceptance of it. `scripts/probe_terac.py` is the thing
that proves the server agrees; this file only prevents us from regressing away from what the
spec says.
"""

from __future__ import annotations

import pytest

from app.clients.terac import (
    EXPECTED_DAYS,
    ROUND2_EXCLUSION_SLUG,
    TASK_TYPE,
    build_round1_payload,
    build_round2_payload,
    participant_task_url,
)


@pytest.fixture
def round1():
    return build_round1_payload(
        project_id="proj_123",
        task_url="https://overwatch.example/t/r1/scan_1?pid={{participant_id}}",
        num_participants=12,
        n_findings=20,
        findings_per_participant=5,
    )


@pytest.fixture
def round2():
    return build_round2_payload(
        project_id="proj_123",
        task_url="https://overwatch.example/t/r2/scan_1?pid={{participant_id}}",
        num_participants=35,
        round1_opportunity_id="opp_round1",
    )


class TestDocumentedConstraints:
    def test_task_type_is_a_member_of_the_enum(self, round1):
        """`survey` is not in the enum and would 400 on every call (RESEARCH.md §10.1)."""
        assert TASK_TYPE in {"interview", "file_upload", "activity"}
        assert round1["tasks"][0]["task_type"] == TASK_TYPE

    def test_expected_days_meets_the_documented_minimum(self, round1, round2):
        """The spec sets a minimum of 5 on `expected_days_to_complete`."""
        assert EXPECTED_DAYS >= 5
        assert round1["expected_days_to_complete"] >= 5
        assert round2["expected_days_to_complete"] >= 5

    def test_cross_quotas_are_accompanied_by_screening_questions(self, round1):
        """`cross_quotas` without `screening_questions` returns BAD_REQUEST."""
        assert round1["cross_quotas"]
        assert round1["screening_questions"]

    def test_every_quota_condition_names_a_declared_screening_question(self, round1):
        keys = {q["key"] for q in round1["screening_questions"]}
        for quota in round1["cross_quotas"]:
            for condition in quota["conditions"]:
                assert condition["screening_question"] in keys

    def test_every_quota_condition_answer_exists_on_its_question(self, round1):
        by_key = {q["key"]: q for q in round1["screening_questions"]}
        for quota in round1["cross_quotas"]:
            for condition in quota["conditions"]:
                question = by_key[condition["screening_question"]]
                assert condition["answer"] in {a["text"] for a in question["answers"]}

    def test_every_qualify_logic_is_in_the_documented_enum(self, round1, round2):
        """Replaces an earlier test asserting that `must` is invalid on `pick: "any"`.

        That constraint does not exist. The screening-questions guide lists `must` as "valid with
        `one` or `any`" and describes the difference as semantic: on multi-select, `must` means
        this exact answer is required and `must_one_of` means at least one of a group. The test
        was enforcing a fabricated rule, which would have rejected a correct payload.
        docs: https://terac.com/docs/developers/guides/screening-questions
        """
        allowed = {"may", "must", "must_one_of", "reject", "review"}
        for payload in (round1, round2):
            for question in payload["screening_questions"]:
                for answer in question.get("answers", []):
                    assert answer["qualify_logic"] in allowed

    def test_every_choice_question_can_actually_be_passed(self, round1, round2):
        """A screener where every answer rejects screens out the entire population."""
        for payload in (round1, round2):
            for question in payload["screening_questions"]:
                answers = question.get("answers", [])
                if not answers:
                    continue
                assert any(a["qualify_logic"] != "reject" for a in answers), question["key"]

    def test_screening_question_keys_are_unique(self, round1):
        keys = [q["key"] for q in round1["screening_questions"]]
        assert len(keys) == len(set(keys))


class TestRound1:
    def test_device_quota_sets_only_a_desktop_minimum(self, round1):
        """RESEARCH.md §10.8: two interlocked floors let a slow mobile cell stall the round.

        One minimum can be met early; a desktop floor *and* a mobile floor means the round
        waits on whichever cell fills slowest, and we cannot absorb that.
        """
        assert len(round1["cross_quotas"]) == 1
        quota = round1["cross_quotas"][0]
        assert quota["quota_type"] == "minimum"
        assert 0 < quota["target"] <= round1["num_participants"]

    def test_has_an_attention_check(self, round1):
        """Round 1 pays for judgment. An attention check is how we notice we did not get it."""
        rejects = [
            a
            for question in round1["screening_questions"]
            for a in question["answers"]
            if a.get("qualify_logic") == "reject"
        ]
        assert rejects

    def test_carries_the_participant_placeholder(self, round1):
        assert "{{participant_id}}" in round1["tasks"][0]["task_url"]


class TestRound2:
    def test_excludes_round_one_participants(self, round2):
        """Without this filter, "a fresh panel" is false and the result is contaminated."""
        slugs = {slug for f in round2["filters"] for slug in f}
        assert ROUND2_EXCLUSION_SLUG in slugs

        exclusion = next(f for f in round2["filters"] if ROUND2_EXCLUSION_SLUG in f)
        assert "opp_round1" in str(exclusion[ROUND2_EXCLUSION_SLUG])

    def test_refuses_to_build_without_the_round_one_id(self):
        """docs/AGENTS.md, Recruiter: block rather than launch.

        Enforced in the builder, not only in the prompt — a model that decides to launch
        anyway still cannot produce a payload.
        """
        with pytest.raises(ValueError, match="BLOCKED"):
            build_round2_payload(
                project_id="proj_123",
                task_url="https://overwatch.example/t/r2/scan_1",
                num_participants=35,
                round1_opportunity_id=None,
            )

    def test_does_not_reveal_which_report_is_which(self, round2):
        """A participant who knows which report is the "improved" one is not judging it."""
        text = (round2["title"] + round2["description"]).lower()
        for tell in ("v1", "v2", "ai", "improved", "recalibrat", "better version"):
            assert tell not in text


class TestParticipantTaskUrl:
    def test_embeds_the_placeholder(self):
        url = participant_task_url("https://overwatch.example", "/t/r1/scan_1")
        assert url == "https://overwatch.example/t/r1/scan_1?pid={{participant_id}}"

    def test_tolerates_a_trailing_slash_on_the_base(self):
        assert participant_task_url("https://overwatch.example/", "/t/r1/s") == (
            "https://overwatch.example/t/r1/s?pid={{participant_id}}"
        )
