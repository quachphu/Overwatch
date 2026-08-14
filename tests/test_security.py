"""Tests for the two security boundaries that face untrusted input.

SPECS.md §9. A customer types a URL and we drive a browser at it, and Terac and Whop POST to us
over the open internet. Both are places where "it works" and "it is safe" are different
statements.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import socket
import time

import pytest

from app import security
from app.security import (
    UnsafeTargetError,
    assert_safe_target,
    is_destructive,
    verify_terac_signature,
    verify_whop_signature,
)


@pytest.fixture
def resolves_public(monkeypatch):
    """Make DNS answer with a public address, without touching the network.

    `make test` is the fast unit suite and has to pass on a plane. The private-range cases
    below need no stub — they are literal IPs, which `getaddrinfo` resolves locally.
    """

    def fake(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]

    monkeypatch.setattr(security.socket, "getaddrinfo", fake)


@pytest.fixture
def resolves_nothing(monkeypatch):
    def fake(host, port, *args, **kwargs):
        raise socket.gaierror(8, "nodename nor servname provided, or not known")

    monkeypatch.setattr(security.socket, "getaddrinfo", fake)


class TestAssertSafeTarget:
    """SSRF. The guard resolves DNS and checks the *resolved address*, because a hostname
    check alone is defeated by any name that happens to point at 127.0.0.1."""

    def test_accepts_a_public_https_url(self, resolves_public):
        assert assert_safe_target("https://example.com/checkout").startswith("https://")

    def test_adds_https_to_a_bare_hostname(self, resolves_public):
        assert assert_safe_target("example.com/checkout") == "https://example.com/checkout"

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8000/admin",
            "http://127.0.0.1/",
            "http://[::1]/",
            "http://169.254.169.254/latest/meta-data/",  # cloud instance metadata
            "http://10.0.0.5/internal",
            "http://192.168.1.1/router",
            "http://172.16.0.1/",
        ],
    )
    def test_refuses_private_loopback_and_metadata_targets(self, url):
        with pytest.raises(UnsafeTargetError):
            assert_safe_target(url, allow_http=True)

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "gopher://example.com/",
            "ftp://example.com/",
            "javascript:alert(1)",
        ],
    )
    def test_refuses_non_http_schemes(self, url):
        with pytest.raises(UnsafeTargetError):
            assert_safe_target(url)

    def test_refuses_plain_http_unless_allowed(self, resolves_public):
        with pytest.raises(UnsafeTargetError):
            assert_safe_target("http://example.com/")

    def test_refuses_a_hostname_that_does_not_resolve(self, resolves_nothing):
        with pytest.raises(UnsafeTargetError):
            assert_safe_target("https://this-host-does-not-exist.invalid/")

    def test_refuses_empty_input(self):
        with pytest.raises(UnsafeTargetError):
            assert_safe_target("")

    @pytest.mark.parametrize("url", ["https://exa mple.com/", "https://[oops/", "https://:99/"])
    def test_malformed_input_is_a_refusal_not_a_crash(self, url):
        """These raise ValueError out of urlsplit if left uncaught.

        `/api/scan` turns `UnsafeTargetError` into a 400 and anything else into a 500, so a
        bare ValueError here is a customer pasting a typo and getting "internal server error".
        """
        with pytest.raises(UnsafeTargetError):
            assert_safe_target(url)


class TestIsDestructive:
    """The scanner clicks things on a stranger's site. Clicking "Delete account" is not a
    finding, it is damage we caused."""

    @pytest.mark.parametrize(
        "text",
        ["Delete account", "Cancel subscription", "Remove card", "DELETE", "Deactivate"],
    )
    def test_blocks_destructive_labels(self, text):
        assert is_destructive(text) is True

    @pytest.mark.parametrize("text", ["Add to cart", "Sign in", "Next", "Learn more", ""])
    def test_allows_ordinary_labels(self, text):
        assert is_destructive(text) is False


class TestVerifyTeracSignature:
    """`base64(HMAC-SHA256(secret, timestamp + RAW_BODY))`.

    Signed over raw bytes. Parsing the JSON and re-serialising it changes the bytes and breaks
    verification, which is the failure that looks like "Terac is sending bad signatures".
    """

    secret = "whsec_test_secret"

    def sign(self, body: bytes, timestamp: str) -> str:
        mac = hmac.new(self.secret.encode(), timestamp.encode() + body, hashlib.sha256).digest()
        return base64.b64encode(mac).decode()

    def test_accepts_a_valid_signature(self):
        body = b'{"event":"submission.approved","id":"sub_1"}'
        ts = str(int(time.time()))
        assert verify_terac_signature(body, self.sign(body, ts), ts, self.secret) is True

    def test_rejects_a_tampered_body(self):
        body = b'{"event":"submission.approved","id":"sub_1"}'
        ts = str(int(time.time()))
        signature = self.sign(body, ts)
        tampered = b'{"event":"submission.approved","id":"sub_EVIL"}'
        assert verify_terac_signature(tampered, signature, ts, self.secret) is False

    def test_rejects_a_reserialised_body(self):
        """Same JSON, different bytes. This is the mistake the guard has to catch."""
        body = b'{"a":1,"b":2}'
        ts = str(int(time.time()))
        signature = self.sign(body, ts)
        assert verify_terac_signature(b'{"a": 1, "b": 2}', signature, ts, self.secret) is False

    def test_rejects_the_wrong_secret(self):
        body = b"{}"
        ts = str(int(time.time()))
        mac = hmac.new(b"wrong", ts.encode() + body, hashlib.sha256).digest()
        bad = base64.b64encode(mac).decode()
        assert verify_terac_signature(body, bad, ts, self.secret) is False

    def test_rejects_a_stale_timestamp(self):
        """Replay window. A signature valid forever is a replayable signature."""
        body = b"{}"
        old = str(int(time.time()) - 3600)
        assert verify_terac_signature(body, self.sign(body, old), old, self.secret) is False

    def test_rejects_missing_headers(self):
        body = b"{}"
        ts = str(int(time.time()))
        assert verify_terac_signature(body, None, ts, self.secret) is False
        assert verify_terac_signature(body, self.sign(body, ts), None, self.secret) is False

    def test_rejects_garbage_signature(self):
        ts = str(int(time.time()))
        assert verify_terac_signature(b"{}", "not-base64!!", ts, self.secret) is False


class TestVerifyWhopSignature:
    """Standard Webhooks: `base64(HMAC-SHA256(secret, "{id}.{timestamp}.{raw body}"))`.

    Deliberately close to Terac's scheme and deliberately tested apart from it. Terac signs
    `timestamp + body` with no id and no dots; Whop signs an id and dots. Verifying one payload
    with the other function must fail, and the last test pins that.
    """

    secret = "whsec_whop_test"
    webhook_id = "msg_2abc"

    def sign(self, body: bytes, timestamp: str, *, secret: str | None = None) -> str:
        signed = f"{self.webhook_id}.{timestamp}.".encode() + body
        mac = hmac.new((secret or self.secret).encode(), signed, hashlib.sha256).digest()
        return "v1," + base64.b64encode(mac).decode()

    # Sentinel, because `wid=None` is a case under test and cannot double as "use the default".
    _DEFAULT = object()

    def call(self, body: bytes, sig: str | None, ts: str | None, *, wid: object = _DEFAULT):
        webhook_id = self.webhook_id if wid is self._DEFAULT else wid
        return verify_whop_signature(body, sig, webhook_id, ts, self.secret)

    def test_accepts_a_valid_signature(self):
        body = b'{"type":"payment.succeeded","data":{"metadata":{"order_id":"ord_1"}}}'
        ts = str(int(time.time()))
        assert self.call(body, self.sign(body, ts), ts) is True

    def test_accepts_one_valid_signature_among_several(self):
        """A rotating secret delivers one space-delimited signature per active key."""
        body = b"{}"
        ts = str(int(time.time()))
        header = f"{self.sign(body, ts, secret='old_key')} {self.sign(body, ts)}"
        assert self.call(body, header, ts) is True

    def test_rejects_a_tampered_order_id(self):
        """The metadata is our join key, so tampering with it is the attack that matters."""
        body = b'{"data":{"metadata":{"order_id":"ord_1"}}}'
        ts = str(int(time.time()))
        sig = self.sign(body, ts)
        evil = b'{"data":{"metadata":{"order_id":"ord_VICTIM"}}}'
        assert self.call(evil, sig, ts) is False

    def test_rejects_a_swapped_webhook_id(self):
        """The id is inside the signed string, so it cannot be replayed under another id."""
        body = b"{}"
        ts = str(int(time.time()))
        assert self.call(body, self.sign(body, ts), ts, wid="msg_other") is False

    def test_rejects_a_stale_timestamp(self):
        body = b"{}"
        old = str(int(time.time()) - 3600)
        assert self.call(body, self.sign(body, old), old) is False

    def test_rejects_an_unversioned_signature(self):
        """A bare base64 digest with no `v1,` prefix is not a scheme we accept."""
        body = b"{}"
        ts = str(int(time.time()))
        assert self.call(body, self.sign(body, ts).removeprefix("v1,"), ts) is False

    def test_rejects_missing_headers(self):
        body = b"{}"
        ts = str(int(time.time()))
        sig = self.sign(body, ts)
        assert self.call(body, None, ts) is False
        assert self.call(body, sig, None) is False
        assert self.call(body, sig, ts, wid=None) is False

    def test_rejects_a_non_numeric_timestamp(self):
        body = b"{}"
        assert self.call(body, self.sign(body, "not-a-time"), "not-a-time") is False

    def test_the_two_providers_are_not_interchangeable(self):
        body = b'{"type":"payment.succeeded"}'
        ts = str(int(time.time()))
        whop_sig = self.sign(body, ts)
        assert verify_terac_signature(body, whop_sig.removeprefix("v1,"), ts, self.secret) is False
