"""Superserve sandbox isolation.

Why it exists: we load arbitrary URLs a stranger pasted, and running a browser against
untrusted pages on the same host as our database is the risk SPECS.md §9 names. A microVM is
the isolation boundary, and its egress controls are the documented second layer behind the
SSRF check in `app/security.py`.

Superserve is **third in the cut order** (docs/RUNBOOK.md). This module is therefore an
opt-in wrapper: `USE_SUPERSERVE=false` (the default) runs Playwright locally and the pipeline
is unaffected. Nothing on the critical path imports it.

**Verification status: the package `superserve` 0.8.2 exists on PyPI and is described as
"Python SDK for the Superserve sandbox API".** Its create/exec/stop surface was not confirmed
from live documentation in this session, so this module refuses to invent one — it imports
the SDK lazily and reports honestly when it cannot proceed. `scripts/probe_superserve.py`
makes the one real call that would let this be completed.
"""

from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)


class SuperserveUnavailableError(RuntimeError):
    """Superserve is not configured or its API surface is unverified."""


def available() -> bool:
    if not settings.use_superserve:
        return False
    if not settings.superserve_api_key:
        logger.info("USE_SUPERSERVE is on but SUPERSERVE_API_KEY is unset.")
        return False
    try:
        import superserve  # noqa: F401
    except Exception:
        logger.warning("superserve is not importable. `pip install superserve`.")
        return False
    return True


def require_sandbox() -> None:
    """Fail loudly rather than silently running unisolated.

    If a future change routes scanning through Superserve, this is the guard that makes an
    unavailable sandbox a refusal rather than a quiet downgrade to local execution.
    """
    if not available():
        raise SuperserveUnavailableError(
            "Superserve sandboxing was requested but is not available. Either set "
            "SUPERSERVE_API_KEY and install `superserve`, or set USE_SUPERSERVE=false to "
            "run Playwright locally. Its create/exec/stop API was not verified in this "
            "session — run scripts/probe_superserve.py first."
        )
