"""Tests for the destructive-action guard.

SPECS.md §9 makes this the one safety property that protects someone other than us: the scanner
clicks around a stranger's production app, and a false negative here deletes their data. The
guard is deliberately asymmetric — a false positive costs one skipped click.

Every test below corresponds to a way the guard was previously defeated. They are written
against the *screening* helpers rather than a live browser so they run in the unit suite; the
one property they cannot cover (that the click site actually calls them) is covered by
`TestSafetyKeyDetectsDomShift`, which asserts the fingerprint the click site compares.
"""

from __future__ import annotations

from urllib.parse import urljoin

import pytest

from app.security import is_destructive
from app.sources.playwright_source import (
    _display_label,
    _is_destructive_target,
    _origin_of,
    _safety_key,
)


class TestLabelSourcesAreUnioned:
    """A non-descriptive `aria-label` must not mask destructive visible text.

    The screening chain used to short-circuit on the first non-empty source, so
    `<button aria-label="cta-42">Delete my account</button>` was screened as "cta-42".
    Analytics-instrumented component libraries produce exactly this markup.
    """

    def test_aria_label_does_not_hide_visible_text(self):
        sources = ["cta-42", "Delete my account", None, None]
        assert is_destructive(_display_label(sources)) is False, "precondition: label looks benign"
        assert _is_destructive_target(sources, None) is True

    def test_title_attribute_is_screened(self):
        assert _is_destructive_target(["Go", None, "Purchase now", None], None) is True

    def test_input_submit_value_is_screened(self):
        """`input[type=submit]` has no inner text, so `value` is the only label it has."""
        assert _is_destructive_target([None, "", None, "Delete account"], None) is True

    def test_benign_controls_still_pass(self):
        for sources in (["My orders"], ["About us"], ["Search"], ["View basket"], ["Contact"]):
            assert _is_destructive_target(sources, None) is False, sources


class TestHrefIsScreened:
    def test_destructive_get_route_is_blocked(self):
        """`GET /items/42/delete` still exists on plenty of admin surfaces."""
        assert _is_destructive_target(["Trash"], "/items/42/delete") is True

    def test_benign_href_passes(self):
        assert _is_destructive_target(["My orders"], "/account/orders") is False


class TestTruncationCannotHideTheVerb:
    def test_verb_past_character_80_is_still_caught(self):
        label = (
            "By continuing you agree to our terms and conditions of service, and this will "
            "permanently delete your account"
        )
        assert is_destructive(label[:80]) is False, "precondition: truncation hides the verb"
        assert _is_destructive_target([label], None) is True

    def test_display_label_is_still_truncated(self):
        assert len(_display_label(["x" * 500])) == 80


class TestVocabulary:
    @pytest.mark.parametrize(
        "label",
        [
            "Delete",
            "Remove item",
            "Empty trash",
            "Discard draft",
            "Terminate instance",
            "Pay now",
            "Place order",
        ],
    )
    def test_english(self, label):
        assert is_destructive(label) is True

    @pytest.mark.parametrize(
        "label",
        ["Log out", "Logout", "Sign out", "Sign Out", "Log off"],
    )
    def test_logout_is_blocked(self, label):
        """Not data loss, but it invalidates the session for every later step.

        The remaining findings would then be captured logged-out while their evidence text
        claims to describe a logged-in flow — a quiet evidence-integrity failure.
        """
        assert is_destructive(label) is True

    @pytest.mark.parametrize(
        "label",
        [
            "Löschen",
            "Bezahlen",
            "Supprimer",
            "Payer",
            "Eliminar",
            "Borrar",
            "Excluir",
            "Elimina",
            "Verwijderen",
            "删除",
            "支付",
            "Удалить",
            "Оплатить",
        ],
    )
    def test_non_english(self, label):
        """The guard was English-only, so a scan of any non-English site ran with destructive
        blocking effectively disabled."""
        assert is_destructive(label) is True


class TestOriginComparison:
    ORIGIN = "https://example.com"

    def _leaves_origin(self, href: str) -> bool:
        target = _origin_of(urljoin(f"{self.ORIGIN}/page", href))
        return bool(target) and target != self.ORIGIN

    @pytest.mark.parametrize(
        "href",
        [
            "https://example.com.evil.com/x",  # passed a startswith() prefix check
            "//evil.com/x",  # protocol-relative, did not start with "http"
            "https://evil.com/x",
        ],
    )
    def test_off_origin_is_detected(self, href):
        assert self._leaves_origin(href) is True

    @pytest.mark.parametrize("href", ["/safe/path", "https://example.com/ok", "?q=1"])
    def test_same_origin_is_allowed(self, href):
        assert self._leaves_origin(href) is False


class TestSafetyKeyDetectsDomShift:
    """The fingerprint the click site compares before clicking.

    `_safe_targets` screens the element at DOM index *i*, then the click site re-queries the DOM
    and clicks `elements[i]`. A dismissed cookie banner shifts every later index, so without
    this comparison the scanner could click a control whose label was never screened — having
    logged "Skipping destructive control: Delete account" while doing it.
    """

    def test_key_changes_when_a_different_element_lands_on_the_index(self):
        screened = _safety_key(["My orders", None, None, None], "/orders")
        shifted = _safety_key(["Delete account", None, None, None], "/account/delete")
        assert screened != shifted

    def test_key_is_stable_for_an_unchanged_element(self):
        sources = ["My orders", "My orders", None, None]
        assert _safety_key(sources, "/orders") == _safety_key(list(sources), "/orders")

    def test_key_distinguishes_a_changed_href_alone(self):
        assert _safety_key(["Continue"], "/next") != _safety_key(["Continue"], "/orders/confirm")
