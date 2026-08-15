"""Security controls. SPECS.md §9 calls these non-negotiable; they are enforced here.

Three separate concerns:

1. **SSRF** — we load URLs a stranger pasted. Guard on the *resolved* IP, never the string.
2. **Webhook signatures** — HMAC over raw bytes, constant-time compare, replay window.
3. **Destructive-action blocking** — the scanner must never click "Delete account" or pay for
   something on a site it does not own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import socket
import time
from urllib.parse import urlparse

# Terac's reference verifier rejects anything older than 300s.
# docs: https://terac.com/docs/developers/guides/webhooks
SIGNATURE_MAX_AGE_SECONDS = 300


class UnsafeTargetError(ValueError):
    """The URL resolves somewhere we refuse to send a browser."""


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Everything that is not a routable public address.

    `is_private` alone is not enough: it misses the cloud metadata endpoint on some
    stacks and misses IPv4-mapped IPv6. Each check is listed explicitly so the intent
    survives a future edit.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
            # 169.254.169.254 is link-local so already covered, but the cloud metadata
            # service is the specific thing this control exists to stop, so it is named.
            str(ip) in {"169.254.169.254", "100.100.100.200"},
        )
    )


def assert_safe_target(url: str, *, allow_http: bool = False) -> str:
    """Validate a scan target and return its normalized URL.

    Raises `UnsafeTargetError` on anything we will not scan. The DNS resolution is the
    load-bearing part: `spoof.example.com` is a perfectly ordinary hostname that can
    resolve to 127.0.0.1, so a string blocklist is security theatre.

    Note the residual TOCTOU: DNS could change between this check and the browser's own
    lookup. Superserve egress control is the documented second layer for that
    (SPECS.md §9); this function is the first.
    """
    url = (url or "").strip()
    if not url:
        raise UnsafeTargetError("Empty URL.")

    if "://" not in url:
        # A scheme with no authority — `javascript:alert(1)`, `mailto:x@y`, `data:…`. Prefixing
        # https:// would turn it into `https://javascript:alert(1)`, whose "port" is
        # `alert(1)`, and rejecting it here keeps that out of urlsplit entirely.
        #
        # Schemes do not contain dots in practice, while a bare `example.com:8443/x` does, so
        # the dot is what separates "the user omitted the scheme" from "this is not a URL".
        probe = urlparse(url).scheme
        if probe and "." not in probe:
            raise UnsafeTargetError(f"Scheme {probe!r} is not allowed. https only.")
        url = f"https://{url}"

    # A malformed authority raises ValueError from .hostname/.port rather than returning None.
    # Left uncaught it surfaces as a 500 on /api/scan, where the honest answer is 400.
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UnsafeTargetError(f"Malformed URL: {exc}") from exc

    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if parsed.scheme not in allowed_schemes:
        raise UnsafeTargetError(
            f"Scheme {parsed.scheme!r} is not allowed. https only."
            if not allow_http
            else f"Scheme {parsed.scheme!r} is not allowed."
        )

    if not host:
        raise UnsafeTargetError("URL has no host.")
    if host.endswith(".local") or host == "localhost":
        raise UnsafeTargetError(f"Refusing internal hostname {host!r}.")

    try:
        infos = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise UnsafeTargetError(f"Could not resolve {host!r}: {exc}") from exc

    if not infos:
        raise UnsafeTargetError(f"Could not resolve {host!r}.")

    for info in infos:
        raw_ip = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            raise UnsafeTargetError(f"Unparseable address for {host!r}: {raw_ip!r}") from None
        # Every resolved address must be safe. One bad answer in a round-robin is enough
        # to reject, because we do not control which one the browser picks.
        if _is_forbidden_ip(ip):
            raise UnsafeTargetError(
                f"{host!r} resolves to non-public address {ip} — refusing to scan."
            )

    return url


# ── Webhook signature verification ───────────────────────────────────────────────────


def verify_terac_signature(
    raw_body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
    secret: str,
    *,
    max_age_seconds: int = SIGNATURE_MAX_AGE_SECONDS,
    now: float | None = None,
) -> bool:
    """`base64(HMAC-SHA256(secret, timestamp + raw_body))`.

    docs: https://terac.com/docs/developers/guides/webhooks

    `raw_body` must be the bytes as received. Parsing and re-serializing the JSON changes
    the bytes and the signature will not match — the docs call this out explicitly and it
    is the single easiest way to lose an afternoon.
    """
    if not signature_header or not timestamp_header or not secret:
        return False

    try:
        ts = float(timestamp_header)
    except (TypeError, ValueError):
        return False

    current = time.time() if now is None else now
    if abs(current - ts) > max_age_seconds:
        return False

    signed = timestamp_header.encode("utf-8") + raw_body
    expected = base64.b64encode(hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest())

    try:
        provided = base64.b64decode(signature_header, validate=True)
    except Exception:
        return False

    # Compare decoded bytes so that base64 padding differences do not cause a false
    # negative, and use compare_digest for constant time.
    return hmac.compare_digest(base64.b64decode(expected), provided)


# ── Destructive action blocking ──────────────────────────────────────────────────────

# SPECS.md §9: block submit on anything matching payment/delete/send patterns.
#
# `\b` is Unicode-aware on str patterns in Python 3, so the non-English terms below behave the
# same as the English ones. They are here because the guard was previously English-only, which
# meant a scan of any non-English site ran with destructive blocking effectively disabled.
DESTRUCTIVE_PATTERNS = re.compile(
    r"\b("
    r"delete|remove|destroy|erase|wipe|purge|trash|discard|archive|clear|terminate|"
    r"pay|payment|checkout|purchase|buy|order|subscribe|billing|card|invoice|"
    r"send|submit\s+order|transfer|withdraw|"
    r"cancel\s+(account|subscription)|deactivate|close\s+account|"
    r"unsubscribe|reset|revoke|"
    # Logout is not data loss, but it invalidates the session for every later step, so the
    # remaining findings get captured logged-out while claiming to describe logged-in flows.
    r"log\s?out|sign\s?out|log\s?off|"
    # de, fr, es, pt, it, nl, and the two non-Latin scripts we are most likely to meet.
    r"l[oö]schen|entfernen|bezahlen|bestellen|senden|abmelden|kündigen|"
    r"supprimer|effacer|payer|commander|envoyer|annuler|résilier|déconnexion|"
    r"eliminar|borrar|pagar|comprar|pedido|enviar|cancelar|cerrar\s+sesión|"
    r"excluir|apagar|remover|comprar|encomendar|enviar|sair|"
    r"elimina|cancella|paga|acquista|ordina|invia|esci|"
    r"verwijderen|betalen|bestellen|verzenden|afmelden|"
    r"删除|移除|清空|付款|支付|购买|下单|提交|退出|注销|"
    r"удалить|удалени|очистить|оплатить|купить|заказать|отправить|выйти|отменить"
    r")\b",
    re.IGNORECASE,
)


def is_destructive(label: str) -> bool:
    """True if an interactive element must not be activated.

    Deliberately over-broad. A false positive costs one skipped click; a false negative
    means we deleted a stranger's data.

    Callers should pass the *union* of every label source and the href, untruncated. Screening
    one source, or a string already cut to 80 characters, is how a destructive control gets
    through: `<button aria-label="cta-42">Delete account</button>` is benign on `aria-label`
    alone, and "…this will permanently delete your account" hides the verb past character 80.
    """
    return bool(label and DESTRUCTIVE_PATTERNS.search(label))


# ── Evidence PII screening ───────────────────────────────────────────────────────────

PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "phone": re.compile(r"\b\+?\d{1,2}?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"),
}


def scan_text_for_pii(text: str) -> list[str]:
    """Return the names of PII patterns present in `text`.

    A text-level pre-filter for Critic. It reads console errors and observed strings,
    which is where leaked emails and tokens actually show up. It does **not** look at
    image pixels, so it is a cheap first pass and not the vision check SPECS.md §9
    describes — `app/agents/critic.py` says so where it uses this.
    """
    if not text:
        return []
    return sorted(name for name, pattern in PII_PATTERNS.items() if pattern.search(text))
