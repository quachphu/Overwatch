"""Data model.

Two layers, deliberately separate:

* **Pydantic** — every payload that crosses a process boundary. `CLAUDE.md` "definition of done"
  item 4: if it touches an external payload, it has a Pydantic model.
* **SQLAlchemy** — durable state. The human loop spans hours, so nothing important lives in memory.

Field names in the Terac-facing models were read from the live OpenAPI spec
(`docs/terac_openapi.json`), not inferred. See RESEARCH.md §1.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now_utc() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


Severity = Literal["blocker", "major", "minor", "cosmetic"]
IsReal = Literal["clear_yes", "probably", "no", "cant_tell"]

# Ordered worst-first. Used for the v1 baseline ranking and for display.
SEVERITY_RANK: dict[str, int] = {"blocker": 0, "major": 1, "minor": 2, "cosmetic": 3}
SEVERITY_WEIGHT: dict[str, float] = {"blocker": 1.0, "major": 0.75, "minor": 0.45, "cosmetic": 0.2}

# SPECS.md §7: "Confirmed = majority of 3 raters answered clear_yes or probably."
CONFIRMING_ANSWERS: frozenset[str] = frozenset({"clear_yes", "probably"})


# ══════════════════════════════════════════════════════════════════════════════════════
# Pydantic — wire models
# ══════════════════════════════════════════════════════════════════════════════════════


class RawFinding(BaseModel):
    """One candidate bug, as produced by a BugSource. Never mutated after creation.

    The whole experiment rests on v1 and v2 ranking the *same* findings, so a finding is
    immutable once written. Only its ranking changes.
    """

    id: str = Field(default_factory=lambda: new_id("find"))
    scan_id: str
    journey: str
    step_intent: str
    expected: str
    observed: str
    screenshot_before_url: str = ""
    screenshot_after_url: str = ""
    console_errors: list[str] = Field(default_factory=list)
    failed_requests: list[str] = Field(default_factory=list)
    source: Literal["replay", "playwright", "seed"]
    agent_severity: Severity
    agent_confidence: float = Field(ge=0.0, le=1.0)
    # Categories are how the intervention drops whole classes of false positive:
    # SPECS.md §2 "drop finding categories with <30% confirmation".
    category: str = "uncategorized"

    @field_validator("console_errors", "failed_requests", mode="before")
    @classmethod
    def _coerce_list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x) for x in v]


class HumanLabel(BaseModel):
    """One participant's verdict on one finding. Round 1 output."""

    finding_id: str
    submission_id: str | None = None
    participant_id: str
    is_real: IsReal
    severity: int = Field(ge=1, le=5)
    round: int = 1
    note: str | None = None


class PreferenceVote(BaseModel):
    """One participant's forced choice between report v1 and v2. Round 2 output."""

    scan_id: str
    participant_id: str
    submission_id: str | None = None
    chose_version: Literal[1, 2]
    why: str | None = None


class ReportView(BaseModel):
    """A ranked report. `version` 1 is the baseline, 2 is post-recalibration."""

    scan_id: str
    version: Literal[1, 2]
    ranked_finding_ids: list[str]


# ── Terac wire models ────────────────────────────────────────────────────────────────
# Shapes below are transcribed from docs/terac_openapi.json. Extra fields are allowed
# because the API is in beta and "Endpoints and request/response shapes may change
# before general availability" (https://terac.com/docs/developers/guides).


class TeracScreeningAnswer(BaseModel):
    model_config = {"extra": "allow"}

    key: str | None = None
    question: str | None = None
    answer: list[Any] = Field(default_factory=list)
    outcome: Literal["qualify", "reject", "not_important", "review"] | None = None


class TeracSubmission(BaseModel):
    """`GET /opportunities/{id}/submissions` -> data[]. `participant_id` is our join key."""

    model_config = {"extra": "allow"}

    id: str
    opportunity_id: str | None = None
    status: Literal[
        "screen_passed",
        "screened_out",
        "in_progress",
        "awaiting_review",
        "approved",
        "rejected",
        "abandoned",
    ]
    participant_id: str
    created_at: str | None = None
    updated_at: str | None = None
    screening_outcome: Literal["passed", "failed", "review"] | None = None
    screening_answers: list[TeracScreeningAnswer] = Field(default_factory=list)
    dashboard_url: str | None = None


class TeracOpportunity(BaseModel):
    model_config = {"extra": "allow"}

    id: str
    title: str | None = None
    status: Literal["draft", "active", "fulfilled", "paused", "stopped", "completed"] | None = None
    num_participants: int | None = None
    estimated_duration_minutes: int | None = None
    dashboard_url: str | None = None


class TeracWebhookEvent(BaseModel):
    """Webhook body. Only two event types exist today per `GET /hooks/event-types`."""

    model_config = {"extra": "allow"}

    event_type: str
    event_id: str | None = None
    resource_id: str | None = None
    occurred_at: str | None = None
    opportunity_id: str | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None


class TeracQuote(BaseModel):
    model_config = {"extra": "allow"}

    quoteId: str  # noqa: N815 - Terac returns camelCase on /quotes
    totalCost: float  # noqa: N815
    costPerParticipant: float  # noqa: N815
    timelineHours: float  # noqa: N815
    submissionCount: float  # noqa: N815
    expiresAt: str  # noqa: N815


class TeracOrgContext(BaseModel):
    """`GET /organizations/current/context`. `balanceDollars` answers UNKNOWN 3 at runtime."""

    model_config = {"extra": "allow"}

    organizationId: str | None = None  # noqa: N815
    organizationName: str | None = None  # noqa: N815
    balanceDollars: float | None = None  # noqa: N815
    dashboard: dict[str, Any] = Field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════════════════
# SQLAlchemy — durable state
# ══════════════════════════════════════════════════════════════════════════════════════


class Base(DeclarativeBase):
    pass


class Scan(Base):
    __tablename__ = "scans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("scan"))
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    source: Mapped[str] = mapped_column(String(32), default="playwright")
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    band_room_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    released: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    findings: Mapped[list[Finding]] = relationship(back_populates="scan", cascade="all, delete")


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id"), index=True)
    journey: Mapped[str] = mapped_column(Text)
    step_intent: Mapped[str] = mapped_column(Text)
    expected: Mapped[str] = mapped_column(Text)
    observed: Mapped[str] = mapped_column(Text)
    screenshot_before_url: Mapped[str] = mapped_column(Text, default="")
    screenshot_after_url: Mapped[str] = mapped_column(Text, default="")
    console_errors: Mapped[list] = mapped_column(JSON, default=list)
    failed_requests: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(32))
    agent_severity: Mapped[str] = mapped_column(String(16))
    agent_confidence: Mapped[float] = mapped_column(Float)
    category: Mapped[str] = mapped_column(String(64), default="uncategorized")
    pii_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    scan: Mapped[Scan] = relationship(back_populates="findings")

    def to_raw(self) -> RawFinding:
        return RawFinding(
            id=self.id,
            scan_id=self.scan_id,
            journey=self.journey,
            step_intent=self.step_intent,
            expected=self.expected,
            observed=self.observed,
            screenshot_before_url=self.screenshot_before_url,
            screenshot_after_url=self.screenshot_after_url,
            console_errors=list(self.console_errors or []),
            failed_requests=list(self.failed_requests or []),
            source=self.source,  # type: ignore[arg-type]
            agent_severity=self.agent_severity,  # type: ignore[arg-type]
            agent_confidence=self.agent_confidence,
            category=self.category,
        )


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("scan_id", "version", name="uq_report_scan_version"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("rep"))
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    ranked_finding_ids: Mapped[list] = mapped_column(JSON, default=list)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Round(Base):
    """One Terac opportunity. Round 1 verifies findings, round 2 compares reports."""

    __tablename__ = "rounds"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("round"))
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id"), index=True)
    round_no: Mapped[int] = mapped_column(Integer)
    opportunity_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    num_participants: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    request_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    response_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    launched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_completion_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Assignment(Base):
    """Which findings a participant judges, and in which order.

    Persisted rather than computed on request for two reasons. Round 1: it is what
    guarantees exactly `r1_raters_per_finding` raters per finding (RESEARCH.md §10.7).
    Round 2: SPECS.md §5.3 requires the randomized v1/v2 side to be stable per
    participant — recomputing it on a page refresh would be order bias with extra steps.
    """

    __tablename__ = "assignments"
    __table_args__ = (UniqueConstraint("scan_id", "round_no", "slot", name="uq_assignment_slot"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("asg"))
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id"), index=True)
    round_no: Mapped[int] = mapped_column(Integer)
    slot: Mapped[int] = mapped_column(Integer)
    participant_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    finding_ids: Mapped[list] = mapped_column(JSON, default=list)
    # Round 2 only: which report version is rendered on the left.
    left_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Label(Base):
    __tablename__ = "labels"
    __table_args__ = (
        UniqueConstraint("finding_id", "participant_id", "round_no", name="uq_label_once"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("lbl"))
    finding_id: Mapped[str] = mapped_column(ForeignKey("findings.id"), index=True)
    scan_id: Mapped[str] = mapped_column(String(64), index=True)
    submission_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    participant_id: Mapped[str] = mapped_column(String(128), index=True)
    is_real: Mapped[str] = mapped_column(String(16))
    severity: Mapped[int] = mapped_column(Integer)
    round_no: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Preference(Base):
    __tablename__ = "preferences"
    __table_args__ = (UniqueConstraint("scan_id", "participant_id", name="uq_pref_once"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("pref"))
    scan_id: Mapped[str] = mapped_column(String(64), index=True)
    participant_id: Mapped[str] = mapped_column(String(128), index=True)
    submission_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chose_version: Mapped[int] = mapped_column(Integer)
    left_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    why: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class WebhookEvent(Base):
    """Dedup table. PK is `X-Event-ID`, which the docs guarantee stable across retries."""

    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="terac")
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Veto(Base):
    """Critic's blocks. Bursar reads this before releasing anything."""

    __tablename__ = "vetoes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("veto"))
    scan_id: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(Text)
    finding_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cleared: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
