"""Request-scoped SQLite handles must survive FastAPI worker handoff."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from communication.api import get_connection as communication_connection
from notification.api import get_connection as notification_connection


@pytest.mark.parametrize("dependency", [communication_connection, notification_connection])
def test_request_connection_can_be_used_by_another_fastapi_worker(tmp_path, dependency):
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db_path=str(tmp_path / "workers.db")))
    )
    scope = dependency(request)
    connection = next(scope)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            # This is the same worker handoff that occurs between FastAPI's
            # synchronous dependency execution and endpoint execution.
            count, foreign_keys = pool.submit(
                lambda: (
                    connection.execute("SELECT count(*) FROM tenant_inbox").fetchone()[0],
                    connection.execute("PRAGMA foreign_keys").fetchone()[0],
                )
            ).result()
        assert count == 0
        assert foreign_keys == 1
    finally:
        scope.close()
