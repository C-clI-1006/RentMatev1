"""Property Reporting contract (Stage 1 UC7 monthly report) — DRAFT.

Status: DRAFT for team sign-off. Nothing here is mandated yet; every
open decision is marked `[OPEN ...]` or `[PROPOSED ...]` below. This
file is the only file the reporting owner (Sharon) proposes to change
in P0; `db/schema.sql` and other modules are NOT touched by it.

Aligned to Stage 1 report:
- §5.3.7 UC7 (System Generates Monthly Property Activity Report):
  main flow, alternative flows A1 (partial information available) /
  A2 (required information cannot be accessed), five-section report.
- §4.3 FR-PR-01..03. FR-PR-04 (dashboard) is OPTIONAL and is NOT part
  of this contract (P4, unscheduled).
- NFR-PR-01 (every reported value traceable to source records) and
  NFR-PR-02 (report content consistent with source records).
- §7.6 Property Reporting is READ-ONLY over source records: the
  reporting module never mutates maintenance/lease/rent data.
- §11.5 Stage 2 scenarios: normal / partial-data / data-access-failure.
- §6.2 the shared Scheduler fires the monthly slot (UC4/UC5/UC7).

Actors:
- Manager only. UC7 has no tenant / draft / review pipeline; reports
  are generated on schedule or on manager request.
- The triggering manager must be authorised for the property.
  [OPEN Q6 — group] The only candidate binding today is
  `tenant_property.role = 'manager'` (db/schema.sql); confirm the
  source of the authorisation check.

Confirmed baseline (Stage 1, no open question):
- Five fixed sections: maintenance, rent, lease status, outstanding
  (open) tasks, risks.
- Trigger: scheduled monthly run OR manager request.
- Read-only over sources; data first, text second; the LLM never
  invents facts; risks come only from deterministic rules.
- "Available but empty" (EMPTY) and "data unavailable"
  (NOT_AVAILABLE) are DIFFERENT states and must be rendered
  distinctly — neither may be shown as "no problem".

Multi-generation semantics (agreed direction; details marked below):
- The same (property, period) MAY have multiple reports. Regeneration
  after a data update is an intended scenario, not a bug.
- `generation` is a per-(property, period) version counter
  (1, 2, 3, ...). It distinguishes versions; it is NOT the dedup key.
- `event_id` is the stable dedup key and is UNIQUE across all reports
  (unique index on `report.event_id` in the proposed DDL below):
  * scheduled trigger -> a STABLE id derived from the schedule slot,
    e.g. "scheduled:2026-11:<property_id>". A re-fire of the same slot
    must return the existing report, never create a duplicate.
  * manual trigger    -> the client mints the event_id ONCE per
    logical request. Retrying the SAME request (network retry, double
    submit) reuses the original id and returns the existing report
    (idempotent, no duplicate). Only a DELIBERATE regeneration (e.g.
    after a data update) mints a NEW id, which produces the next
    generation.
- Duplicate-request handling (agreed order, interface contract only —
  actual checks are verified when the service is implemented):
  (1) AUTHORISATION FIRST: if the caller has no access to the
      property, return `authorization_denied` immediately — without
      revealing whether an existing report matches the event_id.
  (2) ONLY AFTER passing authorisation, compare the existing
      report's property and period against the request; a mismatch
      returns `event_id_conflict` (ReportGenerationError.code).
  (3) NEVER silently return an old report on mismatch.

Data as-of — [PROPOSED, pending confirmation — see Q11]:
- The current prototype INTENDS to use the freshest data available at
  generation time (当前原型拟使用生成时取得的最新数据). This is a
  PROPOSAL for the current prototype, NOT a Stage 1 mandate.
- `ReportRecord.data_as_of` records the UTC moment at which the
  generation read its sources. It does NOT represent a same-instant
  complete snapshot across sources: sources are read sequentially and
  each reflects the state it held at its own read time, so the
  timestamp must not be presented as if all sources were frozen at
  one instant.
- For the derived sections (open_tasks, risks), data_as_of covers the
  read time of their CONTRIBUTING sources; the derivation itself
  happens after the reads.
- Period-end historical snapshots (reconstructing what each source
  looked like exactly at period end) are NOT supported by any source
  today and remain UN-SUPPORTED / PENDING CONFIRMATION. Do not
  implement or promise them on the basis of this contract.

Data-mandatory policy — [PROPOSED, pending group confirmation]:
- Each of maintenance / rent / lease is SECTION-mandatory: if one is
  NOT_AVAILABLE, the report is still generated with that section
  marked unavailable and the outcome is `partial_success` (UC7 A1
  semantics).
- "If ALL THREE of maintenance, rent and lease are NOT_AVAILABLE,
  stop generation and inform the manager" is a PROPOSAL. It is NOT
  yet an A2 mapping: UC7 A2 only says that generation stops and the
  manager is informed when required information cannot be accessed;
  which information counts as REQUIRED is exactly what is being
  decided here. Do not cite UC7 A2 as if this were settled.

Maintenance in-scope window — [PROPOSED, Kerwin Q2]:
- A request is in scope for the period if ANY of:
  (a) it was created within the period;
  (b) it is still open (non-terminal status) at period end;
  (c) its status changed within the period.
- (c) covers "submitted last month, fixed this month": a pure
  created-in-period filter would miss it. The terminal-status set and
  the in-period status-change source (MaintenanceEvent.occurred_at vs
  maintenance_request.updated_at) must come from the provider.
- The LLM-failure behaviour (option A: keep structured data, narrative
  marked MISSING, outcome stays at least partial_success) is
  RECOMMENDED but not yet mandated; see ReportNarrative.
- Enum drift: this contract uses the contract enums from
  contracts/maintenance.py. db/schema.sql (DRAFT v0.1) currently has
  different priority/status values; the provider must serve contract
  enum values, and the schema alignment is owned by the maintenance
  module. [OPEN Q4 — Kerwin + Chaney]

Open items are collected at the bottom of this file.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, NewType, Union

from pydantic import BaseModel, Field, model_validator

from .common import EventId, LeaseId, PropertyId, RentRecordId, RequestId, TenantId
# Reporting re-uses the maintenance contract enums as the canonical
# status/priority vocabulary for reported rows. If the provider cannot
# serve these values (schema drift), that is a provider defect.
# [OPEN Q4 — Kerwin + Chaney]
from .maintenance import MaintenanceStatus, Priority

# [NOTE] Defined here so this draft does not touch other files; when
# the contract is approved, ReportId should move to contracts/common.py
# next to the other IDs.
ReportId = NewType("ReportId", str)

# [NOTE] Also local to this draft for the same reason. An authenticated
# principal, resolved by the layer ABOVE the service (the API / internal
# invocation path) from the SERVER-SIDE auth context. It is an
# INPUT to the service methods below, never derived from fields the
# caller sends in the request payload. A trusted internal path such as
# the shared Scheduler is itself a caller identity (e.g. the system
# identity "system:scheduler", the same vocabulary as
# ReportRecord.created_by).
CallerIdentity = NewType("CallerIdentity", str)


class ReportPeriod(BaseModel):
    """The calendar month a report covers (Stage 1: monthly report)."""

    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)

    @property
    def label(self) -> str:
        """e.g. "2026-11" — matches rent_record.period (schema)."""
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def start(self) -> datetime:
        """Inclusive period start (UTC midnight of the first day)."""
        return datetime(self.year, self.month, 1, tzinfo=timezone.utc)

    @property
    def end(self) -> datetime:
        """Exclusive period end (UTC midnight of the next month)."""
        if self.month == 12:
            return datetime(self.year + 1, 1, 1, tzinfo=timezone.utc)
        return datetime(self.year, self.month + 1, 1, tzinfo=timezone.utc)


class ScheduledReportTrigger(BaseModel):
    """The shared Scheduler fires once per property per month (§6.2).

    The event_id is STABLE for the schedule slot; a re-fire of the same
    slot must return the existing report, never create a duplicate.

    Invocation path (interface contract): the scheduler reaches the
    reporting service through a DEDICATED, TRUSTED INTERNAL invocation
    path (in-process / service-to-service), carrying the trusted system
    identity (e.g. "system:scheduler"). It carries NO manager_id by
    design, and that must NOT block generation. Ordinary HTTP requests
    cannot impersonate the scheduler: the scheduled trigger is only
    reachable through that trusted path, and the server never derives
    it from an external request payload.
    """

    trigger: Literal["scheduled"] = "scheduled"
    event_id: EventId

    @classmethod
    def for_slot(
        cls, property_id: PropertyId, period: ReportPeriod
    ) -> ScheduledReportTrigger:
        """Stable event_id for one schedule slot, e.g.
        "scheduled:2026-11:prop_1".
        """
        return cls(event_id=EventId(f"scheduled:{period.label}:{property_id}"))


class ManagerReportTrigger(BaseModel):
    """A manager requests a report for a property they can access.

    event_id semantics:
    - minted ONCE per logical request (by the client);
    - retrying the SAME request (network retry, double submit) reuses
      the original event_id -> the existing report is returned;
    - a DELIBERATE regeneration (e.g. after a data update) mints a NEW
      event_id -> the next generation is produced.

    manager_id semantics: an identity FIELD, not an identity SOURCE.
    It must MATCH the authenticated caller identity that the server
    resolves from its auth context (the `caller` argument of
    IReportService.generate); the request field is checked against that
    trusted identity, never trusted on its own.
    """

    trigger: Literal["manager"] = "manager"
    manager_id: str  # must match the trusted caller identity (see IReportService.generate)
    event_id: EventId
    reason: str | None = None  # e.g. "regeneration after data update"


ReportTrigger = Annotated[
    Union[ScheduledReportTrigger, ManagerReportTrigger],
    Field(discriminator="trigger"),
]


class ReportSection(str, Enum):
    """The five fixed sections of the Stage 1 monthly report (UC7)."""

    MAINTENANCE = "maintenance"
    RENT = "rent"
    LEASE = "lease"
    OPEN_TASKS = "open_tasks"  # outstanding tasks
    RISKS = "risks"


class SectionAvailability(str, Enum):
    """Per-section data status — MUST be rendered distinctly, never
    collapsed into one another:
    - AVAILABLE: data was collected;
    - EMPTY: source reachable, zero records in scope -> "本周期无记录"
      (no records this period), never "no problem";
    - NOT_AVAILABLE: source unreachable/failed -> "数据不可用" (data
      unavailable) with a note, never "no problem" or "no records".
    """

    AVAILABLE = "available"
    EMPTY = "empty"
    NOT_AVAILABLE = "not_available"


class SourceReference(BaseModel):
    """Provenance of a reported value (NFR-PR-01). Mirrors the shape of
    contracts.lease_rent.FactReference."""

    source_type: str  # "maintenance_request" | "rent_record" | "lease"
    source_id: str
    version: str | None = None
    snippet: str | None = None


def _validate_section_state(
    availability: SectionAvailability,
    records: list,
    note: str | None,
    *,
    label: str,
) -> None:
    """Shared CONFIRMED invariants for data-carrying sections
    (maintenance / rent / lease), checked by each section's
    `model_validator`:

    - NOT_AVAILABLE requires a human-readable `note` explaining the
      unavailability (never bare "data unavailable" silence);
    - EMPTY means the source was reachable and held zero in-scope
      records, so no records may be attached.
    """
    if availability is SectionAvailability.NOT_AVAILABLE and not (
        note is not None and note.strip()
    ):
        raise ValueError(
            f"{label} section is NOT_AVAILABLE and must carry a note "
            "explaining the unavailability"
        )
    if availability is SectionAvailability.EMPTY and records:
        raise ValueError(
            f"{label} section is EMPTY and must not contain records"
        )


class MaintenanceRequestRef(BaseModel):
    """One in-scope maintenance request, as reported.

    Every field is traceable to the maintenance module (NFR-PR-01);
    status/priority use the contract enums from maintenance.py.
    """

    request_id: RequestId
    title: str
    status: MaintenanceStatus
    priority: Priority
    created_at: datetime
    last_activity_at: datetime  # latest MaintenanceEvent.occurred_at
    references: list[SourceReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> "MaintenanceRequestRef":
        # CONFIRMED (NFR-PR-01): every reported record must carry at
        # least one source reference.
        if not self.references:
            raise ValueError(
                "reported maintenance request must carry at least one "
                "source reference (NFR-PR-01)"
            )
        return self


class MaintenanceSectionData(BaseModel):
    """Maintenance section: activity within the period.

    In-scope rule [PROPOSED — Kerwin Q2]: a request is in scope if ANY
    of:
      a) it was created within the period;
      b) it is still open (non-terminal status) at period end;
      c) its status changed within the period.
    (c) covers "submitted last month, fixed this month"; a pure
    created-in-period filter would miss it. (b) and (c) need the
    terminal-status set and the in-period status-change source
    (MaintenanceEvent.occurred_at vs maintenance_request.updated_at)
    from the provider.
    """

    availability: SectionAvailability
    requests: list[MaintenanceRequestRef] = Field(default_factory=list)
    note: str | None = None  # required when NOT_AVAILABLE

    @model_validator(mode="after")
    def _check_section_state(self) -> "MaintenanceSectionData":
        _validate_section_state(
            self.availability, self.requests, self.note, label="maintenance"
        )
        return self


class RentRecordRef(BaseModel):
    """One rent record for the report month (schema: rent_record)."""

    rent_record_id: RentRecordId
    period: str  # "YYYY-MM", matches rent_record.period
    amount_due: Decimal | None = None
    amount_paid: Decimal | None = None
    status: str  # schema: unpaid | partial | paid | overdue
    references: list[SourceReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> "RentRecordRef":
        # CONFIRMED (NFR-PR-01): every reported record must carry at
        # least one source reference.
        if not self.references:
            raise ValueError(
                "reported rent record must carry at least one source "
                "reference (NFR-PR-01)"
            )
        return self


class RentSectionData(BaseModel):
    availability: SectionAvailability
    records: list[RentRecordRef] = Field(default_factory=list)
    # [OPEN Q — Ziheng #3] which rent statuses count as "unpaid/
    # overdue" for the outstanding-task derivation: proposed
    # status in ("unpaid", "overdue") — confirm.
    note: str | None = None  # required when NOT_AVAILABLE

    @model_validator(mode="after")
    def _check_section_state(self) -> "RentSectionData":
        _validate_section_state(
            self.availability, self.records, self.note, label="rent"
        )
        return self


class LeaseRef(BaseModel):
    """One lease relevant to the property (schema: lease).

    NOTE: the `lease` table has no status column and no amount field.
    [OPEN Q — Ziheng #1] `status` is proposed to be DERIVED from
    start_date/end_date vs. the period (e.g. active / expiring /
    expired / unknown) — or Ziheng may add a real status field to the
    provider. Amount is deliberately out of scope for Stage 1.
    """

    lease_id: LeaseId
    tenant_id: TenantId | None = None
    start_date: date
    end_date: date | None = None
    status: str  # derived or provider-provided; see NOTE
    references: list[SourceReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> "LeaseRef":
        # CONFIRMED (NFR-PR-01): every reported record must carry at
        # least one source reference.
        if not self.references:
            raise ValueError(
                "reported lease must carry at least one source "
                "reference (NFR-PR-01)"
            )
        return self


class LeaseSectionData(BaseModel):
    availability: SectionAvailability
    leases: list[LeaseRef] = Field(default_factory=list)
    note: str | None = None  # required when NOT_AVAILABLE

    @model_validator(mode="after")
    def _check_section_state(self) -> "LeaseSectionData":
        _validate_section_state(
            self.availability, self.leases, self.note, label="lease"
        )
        return self


class OpenTaskRef(BaseModel):
    """One outstanding task, derived DETERMINISTICALLY from the three
    sources (no LLM).

    Proposed derivation [OPEN — group]:
    - maintenance: request still open at period end (non-terminal);
    - rent:        rent record with unpaid/overdue status for the
                   period [Ziheng #3];
    - lease:       lease action still pending, e.g. expiring/unknown
                   status [Ziheng #1].
    """

    source: Literal["maintenance", "rent", "lease"]
    source_id: str  # RequestId | RentRecordId | LeaseId
    summary: str
    since: datetime | None = None
    references: list[SourceReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> "OpenTaskRef":
        # CONFIRMED (NFR-PR-01): every reported record must carry at
        # least one source reference.
        if not self.references:
            raise ValueError(
                "reported open task must carry at least one source "
                "reference (NFR-PR-01)"
            )
        return self


class OpenTasksSectionData(BaseModel):
    """Availability is DERIVED: not_available only if ALL THREE
    contributing sources were not_available; otherwise available/empty,
    with a `note` listing which sources contributed. [PROPOSED rule —
    group] An EMPTY open-tasks section means "no open task found in the
    data we could see" — it must not be rendered as "no problem" if any
    source was unavailable."""

    availability: SectionAvailability
    items: list[OpenTaskRef] = Field(default_factory=list)
    note: str | None = None  # required when NOT_AVAILABLE

    @model_validator(mode="after")
    def _check_section_state(self) -> "OpenTasksSectionData":
        _validate_section_state(
            self.availability, self.items, self.note, label="open_tasks"
        )
        return self


class RiskRule(str, Enum):
    """Deterministic risk rules — the ONLY allowed risk source.

    The LLM must never invent or edit risks (Stage 1: risks are
    rule-derived; NFR-PR-02). The rule list itself is [OPEN — group
    sign-off]: these three are the starting proposal; thresholds and
    the full list are not finalised.
    """

    OVERDUE_RENT = "risk.overdue_rent"  # rent record status overdue
    LEASE_EXPIRING = "risk.lease_expiring"  # end_date within N days [N TBC]
    OPEN_EMERGENCY = "risk.open_emergency"  # emergency request not closed


class RiskItem(BaseModel):
    rule_id: RiskRule
    severity: Literal["low", "medium", "high"]
    summary: str  # generated from the rule's deterministic template
    references: list[SourceReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> "RiskItem":
        # CONFIRMED (NFR-PR-01 / NFR-PR-02): a reported risk is a
        # reported value and must cite the source record it fired on.
        if not self.references:
            raise ValueError(
                "reported risk must carry at least one source reference "
                "(NFR-PR-01)"
            )
        return self


class RisksSectionData(BaseModel):
    """Availability is DERIVED like open_tasks: not_available only when
    all contributing sources are not_available. An EMPTY risks section
    means "no rule fired on the data we could see" and must say so —
    not "there are no risks" if any source was unavailable."""

    availability: SectionAvailability
    risks: list[RiskItem] = Field(default_factory=list)
    note: str | None = None  # required when NOT_AVAILABLE

    @model_validator(mode="after")
    def _check_section_state(self) -> "RisksSectionData":
        _validate_section_state(
            self.availability, self.risks, self.note, label="risks"
        )
        return self


class NarrativeSource(str, Enum):
    LIVE = "live"
    STUB = "stub"  # labelled test double — never presented as live
    MISSING = "missing"  # LLM failed; structured data preserved


class ReportNarrative(BaseModel):
    """Free-text summary produced by the LLM summariser.

    LLM failure handling [PROPOSED option A — Chaney, pre-P2]: keep
    the structured sections, mark the narrative MISSING, and the
    outcome stays at least PARTIAL_SUCCESS. Backed by Stage 1 §11.5
    ("preserve the original maintenance, rent, and lease-status
    information") but NOT yet a mandated behaviour; alternatives on
    the table: B) deterministic template text; C) one retry, then A.
    """

    source: NarrativeSource
    text: str | None = None  # None when source is MISSING
    generated_at: datetime | None = None

    @model_validator(mode="after")
    def _check_text(self) -> "ReportNarrative":
        # CONFIRMED: a MISSING narrative (LLM failed) must not carry
        # text — any prose here would masquerade as a generated summary.
        if self.source is NarrativeSource.MISSING and self.text is not None:
            raise ValueError(
                "narrative source is MISSING and must not carry text"
            )
        return self


class ReportOutcome(str, Enum):
    """Maps 1:1 to Stage 1 §11.5 test scenarios:
    - COMPLETE:        all sections available (normal scenario);
    - PARTIAL_SUCCESS: some section(s) not_available, report generated
                       (partial-data scenario; UC7 A1);
    - FAILED:          generation stopped; manager informed
                       (data-access-failure scenario; see the
                       [PROPOSED] mandatory policy in the module
                       docstring — which case yields FAILED is not
                       final until the group confirms).
    """

    COMPLETE = "complete"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class ReportRecord(BaseModel):
    """A stored report: one generation for one (property, period).

    The same (property, period) may have MANY generations
    (regeneration after data updates is intended); `generation` is the
    version counter. `event_id` is the unique dedup key (see module
    docstring).

    data_as_of — [PROPOSED, pending confirmation — see Q11]: the UTC
    moment at which this generation read its sources, under the
    prototype proposal to use the freshest data available at
    generation time (当前原型拟使用生成时取得的最新数据). It is NOT a
    claim that all sources form a same-instant complete snapshot:
    sources are read sequentially and each reflects the state it held
    at its own read time (see module docstring "Data as-of"). Period-
    end historical snapshots are not supported; this field never
    back-fills a point-in-time reconstruction.

    Field constraint (CONFIRMED): data_as_of must be timezone-aware
    and is normalised to UTC, per the project's UTC ISO-8601
    timestamp convention; timezone-NAIVE datetimes are rejected.
    """

    report_id: ReportId
    property_id: PropertyId
    period: ReportPeriod
    generation: int = Field(ge=1)
    trigger: ReportTrigger
    event_id: EventId
    outcome: ReportOutcome
    maintenance: MaintenanceSectionData
    rent: RentSectionData
    lease: LeaseSectionData
    open_tasks: OpenTasksSectionData
    risks: RisksSectionData
    narrative: ReportNarrative
    data_as_of: datetime  # UTC; the moment this generation read its sources
    created_at: datetime  # UTC
    created_by: str  # "manager:<id>" | "system:scheduler"

    @model_validator(mode="after")
    def _check_data_as_of_aware_utc(self) -> "ReportRecord":
        # CONFIRMED: reject timezone-NAIVE datetimes; normalise aware
        # datetimes to UTC (project UTC ISO-8601 timestamp convention).
        if self.data_as_of.tzinfo is None:
            raise ValueError(
                "data_as_of must be timezone-aware (naive datetimes "
                "are rejected); project convention is UTC ISO-8601"
            )
        self.data_as_of = self.data_as_of.astimezone(timezone.utc)
        return self

    @model_validator(mode="after")
    def _check_event_id_matches_trigger(self) -> "ReportRecord":
        # CONFIRMED: the report's dedup key must be the trigger's
        # event_id — the same stable key used for idempotency.
        if self.event_id != self.trigger.event_id:
            raise ValueError(
                "report event_id must equal the trigger's event_id "
                f"(got {self.event_id!r} vs trigger {self.trigger.event_id!r})"
            )
        return self

    @model_validator(mode="after")
    def _check_outcome(self) -> "ReportRecord":
        # CONFIRMED: a report may not be COMPLETE while any data source
        # is unavailable (that is PARTIAL_SUCCESS by definition, §11.5).
        if self.outcome is ReportOutcome.COMPLETE:
            unavailable = [
                name
                for name, section in (
                    ("maintenance", self.maintenance),
                    ("rent", self.rent),
                    ("lease", self.lease),
                    ("open_tasks", self.open_tasks),
                    ("risks", self.risks),
                )
                if section.availability is SectionAvailability.NOT_AVAILABLE
            ]
            if unavailable:
                raise ValueError(
                    "outcome is COMPLETE but data source(s) "
                    f"{unavailable} are NOT_AVAILABLE; "
                    "expected PARTIAL_SUCCESS"
                )
        return self


class GenerationRequest(BaseModel):
    """A request to produce (or fetch the existing) report."""

    property_id: PropertyId
    period: ReportPeriod
    trigger: ReportTrigger


class ReportGenerated(BaseModel):
    status: Literal["generated"] = "generated"
    report: ReportRecord
    reused: bool = False  # True: an existing report matched event_id


class ReportGenerationError(BaseModel):
    status: Literal["failed"] = "failed"
    # "authorization_denied" | "event_id_conflict" | "all_sources_unavailable" | "internal"
    # - "authorization_denied": caller has no access to the property —
    #   returned FIRST, before any event_id comparison (see IReportService.generate).
    # - "event_id_conflict": authorisation PASSED, but the event_id
    #   already exists for a DIFFERENT property or period; never
    #   silently return the old report on mismatch.
    code: str
    detail: str | None = None


GenerationResult = Annotated[
    Union[ReportGenerated, ReportGenerationError],
    Field(discriminator="status"),
]


class IMaintenanceReportSource(ABC):
    """Batch read interface that reporting needs from the maintenance
    module.

    [DRAFT — Kerwin Q1] IMaintenanceTracking today offers a
    single-request `get` only; reporting must not pull requests one by
    one. Proposed here; on approval it either stays here or moves to
    contracts/maintenance.py (Kerwin's choice).

    No direct table reads: reporting consumes this interface only
    (module-boundary rule).
    """

    @abstractmethod
    def list_for_period(
        self, property_id: PropertyId, period: ReportPeriod
    ) -> MaintenanceSectionData:
        """Return all in-scope requests for the period (in-scope rule
        in MaintenanceSectionData) with per-request `last_activity_at`.

        - reachable, zero in-scope requests -> availability=EMPTY
        - source failure                    -> NOT_AVAILABLE + note
        Must not raise for a data-level problem; encode it in the
        availability so reporting can mark the section honestly.
        """


class LeaseRentPeriodData(BaseModel):
    """Rent and lease summaries for one period. Kept as two independent
    sections: a rent failure must not kill the lease section."""

    rent: RentSectionData
    lease: LeaseSectionData


class ILeaseRentReportSource(ABC):
    """Batch read interface that reporting needs from the lease & rent
    module.

    [DRAFT — Ziheng #1] ILeaseRentQuery.get_facts is scoped to a
    tenant question; reporting needs a property/period batch summary.
    No direct table reads: reporting consumes this interface only.
    """

    @abstractmethod
    def period_summary(
        self, property_id: PropertyId, period: ReportPeriod
    ) -> LeaseRentPeriodData:
        """rent: records where rent_record.period == period.label;
        lease: leases relevant to the property. Each section carries
        its own availability (EMPTY vs NOT_AVAILABLE) and note."""


class IReportService(ABC):
    """Public interface of the reporting module (Stage 1 UC7).

    Callers: the shared Scheduler (monthly slot) and the manager API.

    Authorisation (service layer — interface contract only this round;
    actual checks are verified when the service is implemented):
    - The SERVICE layer checks the caller's access to the property,
      before any source or report read, for: generation, viewing one
      report (get), and viewing historical versions (list_for_period).
    - The trusted identity is an INDEPENDENT input — `caller` —
      resolved by the layer above the service from the SERVER-SIDE
      auth context. Identity fields carried in the request payload
      (e.g. trigger.manager_id) are NEVER trusted on their own.
    - Source of the property-access binding: [OPEN Q6 — group].
      Candidate today: tenant_property.role = 'manager'.
    - Scheduled triggers use a DEDICATED TRUSTED INTERNAL invocation
      path (see ScheduledReportTrigger): the absence of a manager_id
      must not block them, and ordinary requests cannot impersonate
      the scheduler.
    """

    @abstractmethod
    def generate(
        self, request: GenerationRequest, caller: CallerIdentity
    ) -> GenerationResult:
        """Authorise, then generate — or return the existing report.

        `caller`: the authenticated principal, supplied by the service
        layer's caller (API / trusted internal path) from the
        server-side auth context. For a manager trigger, the service
        compares it against `trigger.manager_id` before doing anything
        else — a request may not establish its own identity.

        Rules (CHECK ORDER IS PART OF THE CONTRACT):
        1. Authorisation FIRST: the caller must have access to the
           property (manager trigger: trusted caller identity vs
           trigger.manager_id, plus property access; scheduled trigger:
           trusted internal path only). On failure return
           `authorization_denied` IMMEDIATELY — do not compare
           event_id yet, and do not reveal whether a matching report
           exists. [OPEN Q6 — group: source of the property-access
           check]
        2. Duplicate check AFTER authorisation: if
           `trigger.event_id` already exists in the `report` table,
           compare the stored report's property and period against the
           request. Match -> return the stored report (reused=True) —
           never regenerate. Mismatch -> `event_id_conflict`. Never
           silently return an old report on mismatch.
        3. New report: generation = existing count for (property,
           period) + 1; UNIQUE (property_id, period, generation).
        4. Outcome: per the [PROPOSED] mandatory policy in the module
           docstring; `failed` must still emit an audit event.
        5. Never mutates source records (Stage 1 §7.6 read-only).
        6. Emits AuditEvent "report.generated" / "report.failed"
           (contracts/audit.py).
        """

    @abstractmethod
    def get(
        self,
        report_id: ReportId,
        property_id: PropertyId,
        caller: CallerIdentity,
    ) -> ReportRecord | None:
        """Property-scoped read of ONE report; None if absent, or if
        the caller (trusted identity, same source as generate) has no
        access to the property."""

    @abstractmethod
    def list_for_period(
        self,
        property_id: PropertyId,
        period: ReportPeriod,
        caller: CallerIdentity,
    ) -> list[ReportRecord]:
        """ALL generations for the period, ascending `generation` —
        historical versions included; the caller's access to the
        property is checked with the same trusted identity source."""


# ---------------------------------------------------------------------------
# PROPOSED DDL — comment only. NOT applied to db/schema.sql in P0; the
# schema change ships as a reviewable proposal after contract sign-off.
# ---------------------------------------------------------------------------
#
# CREATE TABLE IF NOT EXISTS report (
#     report_id      TEXT PRIMARY KEY,
#     property_id    TEXT NOT NULL REFERENCES property(property_id),
#     period_year    INTEGER NOT NULL,
#     period_month   INTEGER NOT NULL CHECK (period_month BETWEEN 1 AND 12),
#     generation     INTEGER NOT NULL CHECK (generation >= 1),
#     trigger_kind   TEXT NOT NULL CHECK (trigger_kind IN ('scheduled', 'manager')),
#     event_id       TEXT NOT NULL,
#     manager_id     TEXT,                      -- NULL for scheduled
#     outcome        TEXT NOT NULL CHECK (outcome IN
#                                    ('complete', 'partial_success', 'failed')),
#     data_json      TEXT NOT NULL,             -- the five section payloads
#     narrative_json TEXT NOT NULL,             -- ReportNarrative
#     data_as_of     TEXT NOT NULL,             -- [PROPOSED Q11] UTC ISO-8601,
#                                               -- aware only; moment the
#                                               -- generation read its sources
#     created_at     TEXT NOT NULL,             -- UTC ISO-8601
#     created_by     TEXT NOT NULL,
#     UNIQUE (property_id, period_year, period_month, generation)
# );
#
# CREATE UNIQUE INDEX IF NOT EXISTS uq_report_event_id ON report(event_id);
# CREATE INDEX IF NOT EXISTS ix_report_property_period
#     ON report(property_id, period_year, period_month);
#
# ---------------------------------------------------------------------------
# OPEN ITEMS (tracked in the confirmation list sent to the team)
# ---------------------------------------------------------------------------
# Q1 [Kerwin]     IMaintenanceReportSource.list_for_period — accepted,
#                 and does it stay in reporting.py or move to
#                 maintenance.py?
# Q2 [Kerwin]     In-scope window: three-way OR (created-in-period /
#                 open at period end / status changed in period);
#                 terminal-status set; in-period status-change source
#                 (MaintenanceEvent.occurred_at vs maintenance_request
#                 .updated_at).
# Q3 [Ziheng]     ILeaseRentReportSource.period_summary shape; EMPTY vs
#                 NOT_AVAILABLE per section.
# Q4 [Kerwin + Chaney] Enum drift: schema (DRAFT v0.1) priority/status
#                 values differ from the contract enums; provider must
#                 serve contract values; schema aligned by maintenance
#                 owner.
# Q5 [group]      Two-level mandatory policy: per-section mandatory
#                 (A1 semantics) + "all three missing -> stop" as a
#                 PROPOSAL (not yet an A2 mapping).
# Q6 [group]      Manager authorisation source: tenant_property.role =
#                 'manager' is the only candidate today.
# Q7 [group]      Outstanding-task derivation rule (three sources).
# Q8 [group]      Risk rule list + thresholds (RiskRule is a starting
#                 proposal only).
# Q9 [Ziheng]     Lease status: derived from dates vs a real field;
#                 amount out of scope for Stage 1.
# Q10 [Chaney]    LLM-failure option A recommended (pre-P2); scheduler
#                 stable event_id (pre-P3).
# Q11 [group]     data_as_of semantics: prototype proposal to record the
#                 UTC moment the generation read its sources (freshest
#                 data at generation time; NOT a same-instant complete
#                 snapshot across sources; NOT a Stage 1 mandate).
#                 Period-end historical snapshots remain un-supported /
#                 pending confirmation — confirm scope and rendering.
# ---------------------------------------------------------------------------
