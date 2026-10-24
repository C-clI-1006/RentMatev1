"""Maintenance-owned adapter for the monthly reporting contract."""

from __future__ import annotations

import sqlite3

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

from .store import Store

_TERMINAL = {MaintenanceStatus.CLOSED, MaintenanceStatus.CANCELLED}


class SQLiteMaintenanceReportSource(IMaintenanceReportSource):
    """Read maintenance-owned JSON views without mutating source records."""

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
        except sqlite3.Error:
            return MaintenanceSectionData(
                availability=SectionAvailability.NOT_AVAILABLE,
                note="maintenance data could not be read",
            )

        requests: list[MaintenanceRequestRef] = []
        for row in rows:
            try:
                view = MaintenanceView.model_validate_json(row["document"])
            except (ValueError, TypeError):
                return MaintenanceSectionData(
                    availability=SectionAvailability.NOT_AVAILABLE,
                    note="maintenance data contains an invalid record",
                )
            changed_in_period = any(
                period.start <= event.occurred_at < period.end for event in view.events
            )
            if not (
                period.start <= view.created_at < period.end
                or view.status not in _TERMINAL
                or changed_in_period
            ):
                continue
            last_activity = max(
                (event.occurred_at for event in view.events),
                default=view.updated_at,
            )
            requests.append(
                MaintenanceRequestRef(
                    request_id=view.request_id,
                    title=view.title,
                    status=view.status,
                    priority=view.priority,
                    created_at=view.created_at,
                    last_activity_at=last_activity,
                    references=[
                        SourceReference(
                            source_type="maintenance_request",
                            source_id=str(view.request_id),
                            version=str(view.version),
                        )
                    ],
                )
            )
        return MaintenanceSectionData(
            availability=(
                SectionAvailability.AVAILABLE if requests else SectionAvailability.EMPTY
            ),
            requests=requests,
        )
