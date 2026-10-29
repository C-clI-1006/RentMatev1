"""Targeted tests for the maintenance monthly-report period filter.

Locks in the confirmed inclusion rule for ``ReportPeriod`` (half-open
``[month start, next month start)``):

- created in the period, OR
- status changed in the period, OR
- still open (non-terminal) at period end, replayed from the EVENT
  HISTORY — never from the current status.

Therefore a request open at period end but closed in a later month is
still in scope, a request created in the following month is excluded,
and a request whose history cannot establish the period-end status is
included with an explicit note instead of being silently dropped.
"""

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from contracts.maintenance import (
    MaintenanceEvent,
    MaintenanceStatus,
    MaintenanceView,
    Priority,
)
from contracts.reporting import ReportPeriod, SectionAvailability
from maintenance.reporting import SQLiteMaintenanceReportSource
from maintenance.store import Store

PERIOD = ReportPeriod(year=2026, month=9)
PERIOD_DEC = ReportPeriod(year=2026, month=12)


def ts(*args):
    # The named calendar-boundary fixtures describe Sydney business dates.
    return datetime(*args, tzinfo=ZoneInfo("Australia/Sydney")).astimezone(timezone.utc)


def _event(eid, occurred, to_status, from_status=None, request_id="r"):
    return MaintenanceEvent(
        event_id=eid,
        request_id=request_id,
        property_id="p1",
        occurred_at=occurred,
        actor="system:test",
        from_status=from_status,
        to_status=to_status,
    )


def _view(request_id, created, status, *, events=(), priority=None):
    ordered = list(events)
    return MaintenanceView(
        request_id=request_id,
        tenant_id="t1",
        property_id="p1",
        message_id=f"msg-{request_id}",
        title=f"title-{request_id}",
        description="d",
        priority=priority,
        status=status,
        version=1,
        created_at=created,
        updated_at=ordered[-1].occurred_at if ordered else created,
        events=ordered,
    )


def _seed(tmp_path, views):
    path = tmp_path / "mc.db"
    store = Store(path)
    with store.transaction() as db:
        for v in views:
            db.execute(
                "INSERT INTO mc_request VALUES (?,?,?,?,?,?,?,?)",
                (
                    v.request_id,
                    "p1",
                    "t1",
                    v.message_id,
                    f"intake:{v.message_id}",
                    v.version,
                    v.model_dump_json(),
                    json.dumps({"status": "registered"}),
                ),
            )
    return SQLiteMaintenanceReportSource(Store(path))


def _ids(section):
    return {r.request_id for r in section.requests}


def test_sydney_october_edges_include_october_and_exclude_november(tmp_path):
    zone = ZoneInfo("Australia/Sydney")
    views = []
    for month, request_id in [(10, "sydney-october-edge"), (11, "sydney-november-edge")]:
        created = datetime(2026, month, 1, 0, 30, tzinfo=zone).astimezone(timezone.utc)
        closed = datetime(2026, month, 1, 1, 30, tzinfo=zone).astimezone(timezone.utc)
        views.append(_view(request_id, created, MaintenanceStatus.CLOSED, events=(
            _event(request_id + "-created", created, MaintenanceStatus.SUBMITTED, request_id=request_id),
            _event(request_id + "-closed", closed, MaintenanceStatus.CLOSED,
                   MaintenanceStatus.IN_PROGRESS, request_id),
        )))
    section = _seed(tmp_path, views).list_for_period("p1", ReportPeriod(year=2026, month=10))
    assert _ids(section) == {"sydney-october-edge"}


def test_created_next_month_is_excluded_even_if_still_open(tmp_path):
    source = _seed(
        tmp_path,
        [
            _view(
                "oct-created",
                ts(2026, 10, 3),
                MaintenanceStatus.IN_PROGRESS,
                events=(_event("e1", ts(2026, 10, 3), MaintenanceStatus.SUBMITTED, None, "oct-created"),),
            ),
            _view(
                "sep-open",
                ts(2026, 9, 2),
                MaintenanceStatus.IN_PROGRESS,
                events=(_event("e2", ts(2026, 9, 2), MaintenanceStatus.SUBMITTED, None, "sep-open"),),
            ),
        ],
    )

    section = source.list_for_period("p1", PERIOD)

    assert section.availability is SectionAvailability.AVAILABLE
    assert _ids(section) == {"sep-open"}
    # priority=None is preserved as-is on selected rows.
    assert section.requests[0].priority is None


def test_open_at_period_end_but_closed_next_month_is_included(tmp_path):
    source = _seed(
        tmp_path,
        [
            _view(
                "open-at-end",
                ts(2026, 8, 5),
                MaintenanceStatus.CLOSED,  # current (generation-time) status
                priority=Priority.URGENT,
                events=(
                    _event("e1", ts(2026, 8, 5), MaintenanceStatus.SUBMITTED, None, "open-at-end"),
                    _event(
                        "e2",
                        ts(2026, 10, 8),
                        MaintenanceStatus.CLOSED,
                        MaintenanceStatus.IN_PROGRESS,
                        "open-at-end",
                    ),
                ),
            ),
        ],
    )

    section = source.list_for_period("p1", PERIOD)

    # Still in scope because the period-END status (replayed from history)
    # was non-terminal, despite the current status being CLOSED.
    assert _ids(section) == {"open-at-end"}
    ref = section.requests[0]
    assert ref.status is MaintenanceStatus.CLOSED  # latest field at generation time
    assert ref.last_activity_at == ts(2026, 10, 8)


def test_closed_within_period_is_included(tmp_path):
    source = _seed(
        tmp_path,
        [
            _view(
                "closed-in-sep",
                ts(2026, 8, 5),
                MaintenanceStatus.CLOSED,
                events=(
                    _event("e1", ts(2026, 8, 5), MaintenanceStatus.SUBMITTED, None, "closed-in-sep"),
                    _event(
                        "e2",
                        ts(2026, 9, 20),
                        MaintenanceStatus.CLOSED,
                        MaintenanceStatus.IN_PROGRESS,
                        "closed-in-sep",
                    ),
                ),
            ),
        ],
    )

    section = source.list_for_period("p1", PERIOD)

    assert _ids(section) == {"closed-in-sep"}
    assert section.requests[0].status is MaintenanceStatus.CLOSED


def test_ended_before_period_is_excluded(tmp_path):
    source = _seed(
        tmp_path,
        [
            _view(
                "closed-before",
                ts(2026, 7, 10),
                MaintenanceStatus.CLOSED,
                events=(
                    _event("e1", ts(2026, 7, 10), MaintenanceStatus.SUBMITTED, None, "closed-before"),
                    _event(
                        "e2",
                        ts(2026, 8, 15),
                        MaintenanceStatus.CLOSED,
                        MaintenanceStatus.IN_PROGRESS,
                        "closed-before",
                    ),
                ),
            ),
        ],
    )

    section = source.list_for_period("p1", PERIOD)

    assert section.availability is SectionAvailability.EMPTY
    assert section.requests == []
    assert "could not be determined" not in (section.note or "")


def test_month_boundaries_are_half_open(tmp_path):
    source = _seed(
        tmp_path,
        [
            # created exactly at period start (inclusive) -> in scope
            _view(
                "at-start",
                ts(2026, 9, 1, 0, 0, 0),
                MaintenanceStatus.SUBMITTED,
                events=(_event("e1", ts(2026, 9, 1), MaintenanceStatus.SUBMITTED, None, "at-start"),),
            ),
            # created exactly at period end (exclusive) -> excluded
            _view(
                "at-end",
                ts(2026, 10, 1, 0, 0, 0),
                MaintenanceStatus.SUBMITTED,
                events=(_event("e2", ts(2026, 10, 1), MaintenanceStatus.SUBMITTED, None, "at-end"),),
            ),
            # status change exactly at period start (inclusive) -> in scope
            _view(
                "change-at-start",
                ts(2026, 8, 20),
                MaintenanceStatus.IN_PROGRESS,
                events=(
                    _event("e3", ts(2026, 8, 20), MaintenanceStatus.SUBMITTED, None, "change-at-start"),
                    _event(
                        "e4",
                        ts(2026, 9, 1, 0, 0, 0),
                        MaintenanceStatus.IN_PROGRESS,
                        MaintenanceStatus.SUBMITTED,
                        "change-at-start",
                    ),
                ),
            ),
            # closed exactly at period end (exclusive): the close does not
            # count as an in-period change and the period-end status was
            # still non-terminal -> in scope
            _view(
                "closed-at-end",
                ts(2026, 8, 20),
                MaintenanceStatus.CLOSED,
                events=(
                    _event("e5", ts(2026, 8, 20), MaintenanceStatus.SUBMITTED, None, "closed-at-end"),
                    _event(
                        "e6",
                        ts(2026, 10, 1, 0, 0, 0),
                        MaintenanceStatus.CLOSED,
                        MaintenanceStatus.IN_PROGRESS,
                        "closed-at-end",
                    ),
                ),
            ),
        ],
    )

    section = source.list_for_period("p1", PERIOD)

    assert _ids(section) == {"at-start", "change-at-start", "closed-at-end"}


def test_year_boundary_december_to_january(tmp_path):
    source = _seed(
        tmp_path,
        [
            _view(
                "dec-open",
                ts(2026, 12, 20),
                MaintenanceStatus.IN_PROGRESS,
                events=(_event("e1", ts(2026, 12, 20), MaintenanceStatus.SUBMITTED, None, "dec-open"),),
            ),
            _view(
                "jan-created",
                ts(2027, 1, 1, 0, 0, 0),
                MaintenanceStatus.SUBMITTED,
                events=(_event("e2", ts(2027, 1, 1), MaintenanceStatus.SUBMITTED, None, "jan-created"),),
            ),
        ],
    )

    section = source.list_for_period("p1", PERIOD_DEC)

    assert _ids(section) == {"dec-open"}


def test_insufficient_history_is_flagged_not_silently_dropped(tmp_path):
    source = _seed(
        tmp_path,
        [
            # created before the period with NO event history: the
            # period-end status cannot be determined -> include + note
            _view(
                "no-history-open",
                ts(2026, 8, 1),
                MaintenanceStatus.IN_PROGRESS,
                events=(),
            ),
            _view(
                "no-history-closed",
                ts(2026, 8, 2),
                MaintenanceStatus.CLOSED,
                events=(),
            ),
        ],
    )

    section = source.list_for_period("p1", PERIOD)

    assert _ids(section) == {"no-history-open", "no-history-closed"}
    assert section.note is not None
    assert "no-history-open" in section.note
    assert "no-history-closed" in section.note
