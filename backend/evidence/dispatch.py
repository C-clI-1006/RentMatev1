"""Trusted, property-scoped background Maintenance-to-Evidence handoff."""
from __future__ import annotations

import json
import logging
import threading
from functools import partial

from maintenance.outbox import PermanentHandoffError, dispatch_pending
from maintenance.store import Store
from shared import AuthContext

from .maintenance import canonical_event

logger = logging.getLogger(__name__)


def parse_managers(raw: str) -> dict[str, str]:
    """Only deployment configuration supplies the receiving manager identity."""
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not key.strip()
            or not isinstance(manager, str) or not manager.strip()
            for key, manager in value.items()
        ):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ValueError("Evidence dispatch managers must map property IDs to manager IDs") from None


class EvidenceDispatcher:
    """One serial poller per app; existing outbox transactions claim pending rows.

    No network or inference calls are made. The receiver rechecks manager_property
    and atomically persists receipts, timeline entries and evidence links.
    """

    def __init__(self, db_path, service, managers, interval=1.0):
        self.db_path = db_path
        self.service = service
        self.managers = dict(managers)
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._run_lock = threading.Lock()
        self._completed_uncertain = set()

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="evidence-dispatch", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            # Join before the application can release its Evidence connection.
            self._thread.join()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # Never log event payloads, authentication configuration or raw errors.
                logger.warning("Evidence dispatch cycle failed; durable queue retained")
            self._stop.wait(self.interval)

    def run_once(self):
        with self._run_lock:
            store = Store(self.db_path)
            results = []
            for property_id, manager_id in self.managers.items():
                auth = AuthContext(None, property_id, manager_id)
                try:
                    self.service.validate_references([], property_id, auth)
                except PermissionError:
                    # Revocation/configuration error must not consume queued events.
                    continue
                self._recover(store, property_id, auth)
                dispatched = dispatch_pending(
                    store,
                    {"evidence": partial(self.service.receive_maintenance_event, auth=auth)},
                    property_id=property_id,
                )
                for result in dispatched:
                    if result['status'] == 'uncertain':
                        # This worker invoked a synchronous local callback and
                        # observed it unwind; there is no remote request in flight.
                        self._completed_uncertain.add(result['event_id'])
                results.extend(dispatched)
            return results

    def _recover(self, store, property_id, auth):
        with store.transaction() as db:
            rows = db.execute(
                "SELECT event_id,document,status FROM mc_outbox WHERE target='evidence' "
                "AND property_id=? AND status IN ('uncertain','processing')",
                (property_id,),
            ).fetchall()
        for row in rows:
            receipt = self.service.maintenance_receipt(row['event_id'], property_id, auth)
            if receipt['status'] == 'accepted':
                try:
                    _, original = canonical_event(json.loads(row['document']))
                    _, accepted = canonical_event(receipt['payload'])
                    status, error = (('handed_off', None) if original == accepted
                                     else ('rejected', 'event_id_conflict'))
                except (PermanentHandoffError, ValueError):
                    status, error = 'rejected', 'invalid_event'
                eligible = ('uncertain', 'processing')
            elif (row['status'] == 'uncertain'
                  and row['event_id'] in self._completed_uncertain):
                # The synchronous callback has returned. Receipt absence is now
                # verified; conditionally retry only an unchanged uncertain row.
                status, error, eligible = 'retry', None, ('uncertain', 'uncertain')
            else:
                # Another worker or an earlier external adapter may still be
                # running. Receipt absence alone cannot prove safe retry.
                continue
            with store.transaction() as db:
                db.execute(
                    "UPDATE mc_outbox SET status=?,last_error=? WHERE event_id=? "
                    "AND target='evidence' AND property_id=? AND status IN (?,?)",
                    (status, error, row['event_id'], property_id, *eligible),
                )
            self._completed_uncertain.discard(row['event_id'])
