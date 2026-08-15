"""Environment configuration. One place, read once, no magic elsewhere.

Every constant that depends on an open question from RESEARCH.md §9 lives here or in the client
that uses it, carries an `# UNKNOWN:` comment, and is a one-line edit to correct.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from dotenv import load_dotenv

# override=True: python-dotenv's default silently ignores a key that already exists in the
# process environment, even if the existing value is "". Any terminal session where a shell
# profile or an earlier `export` set e.g. WHOP_COMPANY_ID="" would then never see a later edit
# to .env — the file said one thing, the running app read another. .env is meant to be the one
# source of truth (see the file's own header comment), so it must win.
load_dotenv(override=True)


def _flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _str(name: str, default: str) -> str:
    """Read a string setting that has a meaningful default, treating "" as absent.

    For the same reason as `_int`: `os.environ.get(name, default)` honours the default only when
    the key is *missing*. `.env.example` ships several keys blank on purpose — `DATABASE_URL=` is
    the documented "leave empty for the sqlite default" — so copying it verbatim set
    `database_url` to `""`, and `create_engine("")` raises `ArgumentError`. The app did not boot
    at all, from following its own setup instructions.

    Only for settings whose default is a real value. An optional credential stays
    `os.environ.get(...)` returning `None`, because there is no default to fall back to.
    """
    return os.environ.get(name, "").strip() or default


def _int(name: str, default: int) -> int:
    """Read an int, treating a present-but-empty value as absent.

    `os.environ.get(name, default)` returns the default only when the key is *missing*. A key
    set to "" — `R1_PARTICIPANTS=` in a .env file, or a blank field in the Render dashboard —
    returns "" and `int("")` raises. Because these are evaluated in a class body, that failure
    is an ImportError-time crash in every process at once, with a traceback that names the line
    but not the variable.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logging.getLogger(__name__).error(
            "%s=%r is not an integer; falling back to %d.", name, raw, default
        )
        return default


def _float(name: str, default: float) -> float:
    """As `_int`, for floats."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logging.getLogger(__name__).error(
            "%s=%r is not a number; falling back to %s.", name, raw, default
        )
        return default


class Settings:
    """Read-only view of the environment."""

    # ── Terac ──────────────────────────────────────────────────────────────────────
    # docs: https://terac.com/docs/developers/guides — base URL confirmed against the
    # spec's own servers[0].url at https://terac.com/api/external/v2/openapi.json
    terac_base_url: str = _str("TERAC_BASE_URL", "https://terac.com/api/external/v2")
    terac_api_key: str | None = os.environ.get("TERAC_API_KEY")
    terac_webhook_secret: str | None = os.environ.get("TERAC_WEBHOOK_SECRET")
    terac_project_id: str | None = os.environ.get("TERAC_PROJECT_ID")

    # ── Whop ───────────────────────────────────────────────────────────────────────
    whop_api_key: str | None = os.environ.get("WHOP_API_KEY")
    whop_company_id: str | None = os.environ.get("WHOP_COMPANY_ID")
    whop_plan_id: str | None = os.environ.get("WHOP_PLAN_ID")
    whop_webhook_secret: str | None = os.environ.get("WHOP_WEBHOOK_SECRET")

    # ── Superserve / Replay ────────────────────────────────────────────────────────
    superserve_api_key: str | None = os.environ.get("SUPERSERVE_API_KEY")
    replay_api_key: str | None = os.environ.get("REPLAY_API_KEY")
    # Defaults to the server in Replay's own OpenAPI spec; override only to point at a staging
    # host. app/sources/replay.py supplies the default, so an unset value is not an error.
    replay_base_url: str | None = os.environ.get("REPLAY_BASE_URL")

    # ── LLM ────────────────────────────────────────────────────────────────────────
    openai_api_key: str | None = os.environ.get("OPENAI_API_KEY")
    anthropic_api_key: str | None = os.environ.get("ANTHROPIC_API_KEY")
    # Band's own docs wire ChatOpenAI(model="gpt-5.5") in both the hacker guide and
    # AGENTS.md. Kept configurable because an unavailable model name is a startup crash
    # in five processes at once.
    llm_model: str = _str("OVERWATCH_LLM_MODEL", "gpt-5.5")
    llm_fallback_model: str = _str("OVERWATCH_LLM_FALLBACK_MODEL", "gpt-4o")

    # ── App ────────────────────────────────────────────────────────────────────────
    database_url: str = _str("DATABASE_URL", "sqlite:///./overwatch.db")
    public_base_url: str = _str("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    evidence_base_url: str | None = os.environ.get("EVIDENCE_BASE_URL")
    evidence_dir: str = _str("EVIDENCE_DIR", "./evidence")
    scan_price_usd: float = _float("SCAN_PRICE_USD", 49)

    # Shared secret for the operator API — the endpoints that launch Terac rounds and
    # therefore spend real money. Required once the app is publicly reachable; see
    # `is_publicly_reachable` below and `require_operator` in app/main.py.
    operator_token: str | None = os.environ.get("OPERATOR_TOKEN")

    # ── Scanner ────────────────────────────────────────────────────────────────────
    # SPECS.md §3.2: cap 12 steps per journey. Also a target-site abuse control (§9).
    max_steps_per_journey: int = _int("MAX_STEPS_PER_JOURNEY", 12)
    use_superserve: bool = _flag("USE_SUPERSERVE", False)
    bug_source: str = _str("BUG_SOURCE", "playwright")

    # ── Experiment sizing ──────────────────────────────────────────────────────────
    # docs/DECISIONS.md 004: 12 participants x 5 findings = 60 judgments over 20
    # findings at exactly 3 raters each.
    r1_participants: int = _int("R1_PARTICIPANTS", 12)
    r1_findings_per_participant: int = _int("R1_FINDINGS_PER_PARTICIPANT", 5)
    r1_raters_per_finding: int = _int("R1_RATERS_PER_FINDING", 3)
    r2_participants: int = _int("R2_PARTICIPANTS", 35)

    @property
    def evidence_public_base(self) -> str:
        """Base URL for evidence images.

        Terac participants load these from their own phones, so it must be public.
        Falls back to serving them off our own host, which works as long as
        PUBLIC_BASE_URL is public (Render) rather than localhost.
        """
        return (self.evidence_base_url or f"{self.public_base_url}/evidence").rstrip("/")

    @property
    def is_publicly_reachable(self) -> bool:
        """Whether strangers can reach this instance.

        Used to decide when the operator API must be authenticated. Keyed off the configured
        public base URL rather than a separate DEBUG flag, because that is the same value we
        hand to Terac as the participant task host: if it is a real public host, participants
        can reach us, and so can everyone else.

        The consequence is deliberate — an instance on Render cannot serve the money-spending
        endpoints unauthenticated even if someone forgets to set `OPERATOR_TOKEN`, and a laptop
        on localhost stays frictionless.
        """
        base = self.public_base_url.lower()
        # S104 flags "0.0.0.0" as binding to all interfaces. It is a substring we look *for* in a
        # configured URL, not an address we bind.
        local_markers = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "host.docker.internal")  # noqa: S104
        return not any(marker in base for marker in local_markers)

    def require(self, *names: str) -> None:
        """Fail loudly and early rather than 401-ing deep inside a client."""
        missing = [n for n in names if not getattr(self, n, None)]
        if missing:
            raise RuntimeError(
                "Missing required configuration: "
                + ", ".join(n.upper() for n in missing)
                + ". See .env.example."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
