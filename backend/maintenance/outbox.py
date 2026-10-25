"""Handoff only: accepted is not sent. Consumers deduplicate event IDs.
Interrupted or ambiguous handoffs require receipt reconciliation before retry.
"""

import json


class RetryableHandoffError(Exception):
    """Adapter guarantees non-acceptance, making retry safe."""


class PermanentHandoffError(Exception):
    """Definite rejection; retrying the same payload cannot repair it."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def dispatch_pending(store, adapters, limit=50, *, property_id=None):
    results = []
    for _ in range(limit):
        with store.transaction() as db:
            rows = db.execute(
                "SELECT rowid,* FROM mc_outbox WHERE status IN ('pending','retry') ORDER BY rowid"
            ).fetchall()
            row = next((r for r in rows if r["target"] in adapters
                        and (property_id is None or r["property_id"] == property_id)), None)
            if row is None:
                break
            db.execute(
                "UPDATE mc_outbox SET status='processing',attempts=attempts+1 WHERE rowid=?",
                (row["rowid"],),
            )
        try:
            if adapters[row["target"]](json.loads(row["document"])) is not True:
                raise RuntimeError("No definite acknowledgement")
            status, error = "handed_off", None
        except PermanentHandoffError as exc:
            status, error = "rejected", exc.code
        except RetryableHandoffError:
            status, error = "retry", "Consumer confirmed non-acceptance"
        except Exception:  # noqa: BLE001 - plugin boundary must fail closed without leaking secrets
            status, error = "uncertain", "Reconcile receipt before retry"
        with store.transaction() as db:
            db.execute(
                "UPDATE mc_outbox SET status=?,last_error=? WHERE rowid=?",
                (status, error, row["rowid"]),
            )
        results.append({"event_id": row["event_id"], "target": row["target"], "status": status})
        if status == "retry":
            break
    return results


def reconcile(store, event_id, target, *, accepted):
    """Trusted integration only; accepted=False requires verified receipt absence."""
    with store.transaction() as db:
        db.execute(
            "UPDATE mc_outbox SET status=?,last_error=NULL WHERE event_id=? AND target=? AND status IN ('uncertain','processing')",
            ("handed_off" if accepted else "retry", event_id, target),
        )
