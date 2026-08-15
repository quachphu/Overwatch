"""Evidence storage.

The single hard requirement: **these URLs must load for a stranger on a phone.** Terac
participants open them from their own devices, so a path that works on localhost and
nowhere else silently voids both rounds (SPECS.md §3.3).

Files are written to `settings.evidence_dir` and served by FastAPI at `/evidence/...`.
On Render, that directory must be a persistent disk — ephemeral disk wipes on redeploy and
takes every screenshot with it (`CLAUDE.md`, Render gotchas).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from app.config import settings

logger = logging.getLogger(__name__)


def evidence_root() -> Path:
    root = Path(settings.evidence_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def scan_dir(scan_id: str) -> Path:
    directory = evidence_root() / scan_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_screenshot(scan_id: str, filename: str, data: bytes) -> str:
    """Write a screenshot and return its public URL."""
    safe = os.path.basename(filename)
    path = scan_dir(scan_id) / safe
    path.write_bytes(data)
    return public_url(scan_id, safe)


def public_url(scan_id: str, filename: str) -> str:
    return f"{settings.evidence_public_base}/{scan_id}/{os.path.basename(filename)}"


def to_display_path(url: str) -> str:
    """Path-only form of an evidence URL, safe to embed as a same-origin `<img src>`.

    Evidence URLs are stored absolute (module docstring — Terac participants must be able to
    open one from any device). But `report.html` and `t_r1.html` are rendered and *viewed* on
    whatever origin the viewer's browser is currently on: localhost while developing, the ngrok
    domain in the field. An absolute cross-origin `<img src>` pointed at a free-tier ngrok
    tunnel hits ngrok's browser-warning interstitial for any browser that has not already
    clicked through it on that exact domain — the request never reaches our server at all, it
    gets back an HTML warning page (`ERR_NGROK_6024`) instead of image bytes, which renders as a
    blank/broken image. Stripping to a path makes the browser resolve it against whatever origin
    served the page — always this same app — so the image request never leaves that origin and
    the interstitial never triggers. RESEARCH.md §13.23.
    """
    if not url:
        return url
    return urlsplit(url).path or url


def is_public_url(url: str) -> bool:
    """Whether one evidence URL is loadable by a stranger on a phone."""
    return (
        url.startswith("https://")
        and "localhost" not in url
        and "127.0.0.1" not in url
        and "0.0.0.0" not in url  # noqa: S104 — rejecting a private host in a URL, not binding
    )


def is_public_base() -> bool:
    """Whether evidence URLs *written from now on* are reachable from outside this machine.

    Called by the ingress before launching a Terac round so that the failure is a loud
    refusal at 13:20 rather than twelve participants staring at broken images.

    Note the tense. This inspects the *current* setting, so it cannot see that a screenshot
    captured earlier — before `PUBLIC_BASE_URL` was pointed at the Render host — already has a
    `localhost` URL frozen into the database. `is_public_url` is the check for those, and
    `launch_round1` applies it to the rows it is about to buy judgments on.
    """
    return is_public_url(settings.evidence_public_base)
