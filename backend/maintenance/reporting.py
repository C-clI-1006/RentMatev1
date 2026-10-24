"""Maintenance-owned providers for monthly reporting.

Period membership is reconstructed from recorded events. Selected rows expose
the values current at generation time rather than a historical snapshot.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from pydantic import ValidationError

from contracts.common import PropertyId
from contracts.maintenance import MaintenanceStatus, MaintenanceView
from contracts.reporting import (
    IMaintenanceReportSource,
    MaintenanceRequestRef,
    MaintenanceSectionData,
    ReportPeriod,
    SectionAvailability,
    SourceReference,
)
from permissions import MissingManagerIdentityError

from .store import Store

_TERMINAL = {MaintenanceStatus.CLOSED, MaintenanceStatus.CANCELLED}


class MaintenanceReportSource(IMaintenanceReportSource):
    """Manager-scoped provider used by interactive reporting flows."""

    def __init__(self, service) -> None:
        self.service = service

    def list_for_period(
        self, property_id: PropertyId, period: ReportPeriod
    ) -> MaintenanceSectionData:
        if property_id != self.service.auth.property_id:
            raise PermissionError("Property scope mismatch")
        try:
            self.service.require_manager_access()
            return _section_from_requests(self.service.list_requests(), period)
        except MissingManagerIdentityError as error:
            raise PermissionError("Manager identity required") from error
        except (sqlite3.Error, ValidationError):
            return _unavailable()


class SQLiteMaintenanceReportSource(IMaintenanceReportSource):
    """System-scheduler provider over maintenance-owned persisted views.

    Scheduler authentication and property existence are enforced by
    ``ReportService`` before this trusted adapter is called.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def list_for_period(
        self, property_id: PropertyId, period: ReportPeriod
    ) -> MaintenanceSectionData:
        try:
            with self._store.transaction() as connection:
                rows = connection.execute(
                    "SELECT document FROM mc_request WHERE property_id=? ORDER BY id",
                    (str(property_id),),
                ).fetchall()
            requests = [MaintenanceView.model_validate_json(row["document"]) for row in rows]
            return _section_from_requests(requests, period)
        except (sqlite3.Error, ValidationError, ValueError, TypeError):
            return _unavailable()


def _section_from_requests(
    requests: Iterable[MaintenanceView], period: ReportPeriod
) -> MaintenanceSectionData:
    selected: list[MaintenanceRequestRef] = []
    indeterminate: list[str] = []

    for request in requests:
        events = sorted(request.events, key=lambda event: event.occurred_at)
        before_end = [event for event in events if event.occurred_at < period.end]
        created_in_period = period.start <= request.created_at < period.end
        changed_in_period = any(
            period.start <= event.occurred_at < period.end for event in events
        )

        open_at_period_end = False
        if request.created_at < period.end:
            if before_end:
                open_at_period_end = before_end[-1].to_status not in _TERMINAL
            else:
                # A current status cannot establish the historical month-end state.
                indeterminate.append(str(request.request_id))

        if not (created_in_period or changed_in_period or open_at_period_end):
            continue

        selected.append(
            MaintenanceRequestRef(
                request_id=request.request_id,
                title=request.title,
                status=request.status,
                priority=request.priority,
                created_at=request.created_at,
                last_activity_at=max(
                    (event.occurred_at for event in events),
                    default=request.created_at,
                ),
                references=[
                    SourceReference(
                        source_type="maintenance_request",
                        source_id=str(request.request_id),
                        version=str(request.version),
                    )
                ],
            )
        )

    selected.sort(key=lambda row: (row.created_at, row.request_id))
    note = "Period membership uses event history; row values reflect the current read."
    if indeterminate:
        note += (
            " Month-end state could not be determined from event history for: "
            + ", ".join(sorted(indeterminate))
            + "."
        )
    return MaintenanceSectionData(
        availability=(
            SectionAvailability.AVAILABLE if selected else SectionAvailability.EMPTY
        ),
        requests=selected,
        note=note,
    )


def _unavailable() -> MaintenanceSectionData:
    return MaintenanceSectionData(
        availability=SectionAvailability.NOT_AVAILABLE,
        note="Maintenance data could not be read or validated.",
    )
