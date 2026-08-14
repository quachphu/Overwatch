"""The endpoints that spend money must not be open.

`POST /api/scans/{id}/round1` hires twelve people through Terac. It is not idempotent — each
call creates a fresh opportunity — so an open endpoint is a loop over our balance, reachable by
anyone who can see a scan id in a URL.

`require_operator` keys off reachability rather than a debug flag, and these tests pin both
halves of that: frictionless on localhost, closed on a public host.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

# Every route that spends credit, consumes the Terac rate limit, or discloses our balance.
# `POST /api/scan` is deliberately absent: it is the public product ingress.
OPERATOR_ROUTES = [
    ("post", "/api/scans/scan_missing/round1"),
    ("post", "/api/scans/scan_missing/round2"),
    ("post", "/api/scans/scan_missing/v2"),
    ("post", "/api/scans/scan_missing/poll"),
    ("get", "/api/terac/balance"),
]


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def public_no_token(monkeypatch):
    """A publicly reachable instance with nobody having set OPERATOR_TOKEN."""
    monkeypatch.setattr(settings, "public_base_url", "https://overwatch.onrender.com")
    monkeypatch.setattr(settings, "operator_token", None)


@pytest.fixture
def public_with_token(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://overwatch.onrender.com")
    monkeypatch.setattr(settings, "operator_token", "op_secret_token")


@pytest.fixture
def localhost(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    monkeypatch.setattr(settings, "operator_token", None)


class TestPublicHostWithoutToken:
    @pytest.mark.parametrize(("method", "path"), OPERATOR_ROUTES)
    def test_refuses_rather_than_failing_open(self, client, public_no_token, method, path):
        """503, not 200.

        Failing open on a public host is how a Terac balance disappears, so an unset token is
        treated as a misconfiguration to refuse rather than a default to permit.
        """
        response = getattr(client, method)(path)
        assert response.status_code == 503
        assert "OPERATOR_TOKEN" in response.json()["detail"]


class TestPublicHostWithToken:
    @pytest.mark.parametrize(("method", "path"), OPERATOR_ROUTES)
    def test_rejects_missing_token(self, client, public_with_token, method, path):
        assert getattr(client, method)(path).status_code == 401

    @pytest.mark.parametrize(("method", "path"), OPERATOR_ROUTES)
    def test_rejects_wrong_token(self, client, public_with_token, method, path):
        response = getattr(client, method)(path, headers={"X-Operator-Token": "op_wrong_token"})
        assert response.status_code == 401

    def test_rejects_token_that_is_a_prefix_of_the_real_one(self, client, public_with_token):
        """Guards against a length-insensitive or prefix comparison."""
        response = client.post(
            "/api/scans/scan_missing/v2", headers={"X-Operator-Token": "op_secret"}
        )
        assert response.status_code == 401

    def test_accepts_bearer_header(self, client, public_with_token):
        response = client.post(
            "/api/scans/scan_missing/v2",
            headers={"Authorization": "Bearer op_secret_token"},
        )
        # Past the guard: 400 because the scan does not exist, which is the point.
        assert response.status_code == 400

    def test_accepts_x_operator_token_header(self, client, public_with_token):
        response = client.post(
            "/api/scans/scan_missing/v2", headers={"X-Operator-Token": "op_secret_token"}
        )
        assert response.status_code == 400

    def test_bearer_scheme_is_case_insensitive(self, client, public_with_token):
        response = client.post(
            "/api/scans/scan_missing/v2",
            headers={"Authorization": "bearer op_secret_token"},
        )
        assert response.status_code == 400


class TestLocalhost:
    def test_no_token_needed_so_the_rehearsal_still_runs(self, client, localhost):
        """`make rehearse` and the demo must not need a token on a laptop."""
        response = client.post("/api/scans/scan_missing/v2")
        assert response.status_code == 400  # reached the handler

    def test_a_configured_token_is_still_enforced_locally(self, client, monkeypatch):
        """Setting the token locally should not be silently ignored."""
        monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
        monkeypatch.setattr(settings, "operator_token", "op_secret_token")
        assert client.post("/api/scans/scan_missing/v2").status_code == 401


class TestReachabilityDetection:
    @pytest.mark.parametrize(
        "base",
        [
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "http://0.0.0.0:8000",
            "http://[::1]:8000",
            "http://host.docker.internal:8000",
        ],
    )
    def test_local_hosts_are_not_public(self, monkeypatch, base):
        monkeypatch.setattr(settings, "public_base_url", base)
        assert settings.is_publicly_reachable is False

    @pytest.mark.parametrize(
        "base",
        [
            "https://overwatch.onrender.com",
            "https://abc123.ngrok-free.app",
            "http://203.0.113.10",
        ],
    )
    def test_real_hosts_are_public(self, monkeypatch, base):
        monkeypatch.setattr(settings, "public_base_url", base)
        assert settings.is_publicly_reachable is True


class TestPublicRoutesStayOpen:
    """The guard must not have caught the customer-facing routes."""

    def test_healthz_needs_no_token(self, client, public_no_token):
        assert client.get("/healthz").status_code == 200

    def test_results_needs_no_token(self, client, public_no_token):
        """The dashboard reads this on every page load; it spends nothing."""
        assert client.get("/api/scans/scan_missing/results").status_code == 200
