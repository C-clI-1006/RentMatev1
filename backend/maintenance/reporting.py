"""Maintenance-owned report provider. Period membership uses recorded events.

Rows expose current values, not a reconstructed period-end snapshot.
The proposed membership policy must be reviewed with Reporting's owner.
"""

import sqlite3

from pydantic import ValidationError

from contracts.maintenance import MaintenanceStatus as S
from contracts.reporting import (
    IMaintenanceReportSource,
    MaintenanceRequestRef,
    MaintenanceSectionData,
    SourceReference,
)
from contracts.reporting import (
    SectionAvailability as A,
)
from permissions import MissingManagerIdentityError


class MaintenanceReportSource(IMaintenanceReportSource):
    def __init__(self, service):
        self.service = service

    def list_for_period(self, property_id, period):
        if property_id != self.service.auth.property_id:
            raise PermissionError("Property scope mismatch")
        try:
            self.service.require_manager_access()
            rows = []
            for request in self.service.list_requests():
                events = sorted(request.events, key=lambda event: event.occurred_at)
                before_end = [e for e in events if e.occurred_at < period.end]
                created = period.start <= request.created_at < period.end
                changed = any(period.start <= e.occurred_at < period.end for e in events)
                open_at_end = (
                    request.created_at < period.end and before_end
                    and before_end[-1].to_status not in (S.CLOSED, S.CANCELLED)
                )
                if not (created or changed or open_at_end):
                    continue
                rows.append(MaintenanceRequestRef(
                    request_id=request.request_id, title=request.title,
                    status=request.status, priority=request.priority,
                    created_at=request.created_at,
                    last_activity_at=max((e.occurred_at for e in events),
                                         default=request.created_at),
                    references=[SourceReference(
                        source_type="maintenance_request", source_id=request.request_id,
                        version=str(request.version),
                    )],
                ))
            rows.sort(key=lambda row: (row.created_at, row.request_id))
            return MaintenanceSectionData(
                availability=A.AVAILABLE if rows else A.EMPTY, requests=rows,
                note="Period membership uses event history; row values reflect the current read.",
            )
        except MissingManagerIdentityError as error:
            raise PermissionError("Manager identity required") from error
        except (sqlite3.Error, ValidationError):
            return MaintenanceSectionData(
                availability=A.NOT_AVAILABLE,
                note="Maintenance data could not be read or validated.",
            )
