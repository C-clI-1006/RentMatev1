"""Durable human work items, independent of reply drafts and approvals."""

import json
from typing import Literal

from pydantic import BaseModel

from shared import new_id, utcnow
from .service import ReviewService, ReviewConflictError, ReviewNotFoundError, InvalidReviewDecisionError


class HandoffCase(BaseModel):
    case_id: str
    message_id: str
    property_id: str
    original_message: str
    reason: str
    status: Literal['open', 'resolved', 'dismissed']
    created_at: str
    resolver_id: str | None
    resolution: str | None
    resolved_at: str | None


def record_handoff(connection, message_id, reason):
    """One case per persisted message; replays never reopen a settled case."""
    try:
        now = utcnow().isoformat().replace('+00:00', 'Z')
        case_id = new_id()
        cursor = connection.execute("""INSERT INTO communication_handoff
            (case_id,message_id,property_id,reason,created_at)
            SELECT ?,message_id,property_id,?,? FROM message WHERE message_id=?
            ON CONFLICT(message_id) DO NOTHING""", (case_id, reason, now, message_id))
        if cursor.rowcount:
            connection.execute("""INSERT INTO audit
                (audit_id,actor,action,subject_type,subject_id,details,at)
                VALUES (?,'system:communication','handoff.created','handoff',?,?,?)""",
                (new_id(), case_id, json.dumps({'message_id': message_id}), now))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


class HandoffService(ReviewService):
    """Reuse the existing authoritative manager-scope check."""

    def list_cases(self):
        self._require_scope()
        return [HandoffCase(**dict(row)) for row in self._connection.execute(
            _SELECT + ' WHERE h.property_id=? ORDER BY h.created_at,h.case_id', (self.auth.property_id,))]

    def get_case(self, case_id):
        self._require_scope()
        row = self._connection.execute(_SELECT + ' WHERE h.case_id=? AND h.property_id=?',
                                       (case_id, self.auth.property_id)).fetchone()
        if row is None:
            raise ReviewNotFoundError('Handoff case not found')
        return HandoffCase(**dict(row))

    def settle(self, case_id, *, outcome, resolution):
        if outcome not in ('resolved', 'dismissed') or not resolution.strip():
            raise InvalidReviewDecisionError('An outcome and non-empty resolution are required')
        resolution = resolution.strip()
        self._connection.execute('BEGIN IMMEDIATE')
        try:
            manager_id = self._require_scope()
            case = self.get_case(case_id)
            if case.status != 'open':
                if (case.status, case.resolver_id, case.resolution) == (outcome, manager_id, resolution):
                    self._connection.commit()
                    return case
                raise ReviewConflictError('Handoff case already settled')
            now = utcnow().isoformat().replace('+00:00', 'Z')
            self._connection.execute("""UPDATE communication_handoff
                SET status=?,resolver_id=?,resolution=?,resolved_at=? WHERE case_id=? AND status='open'""",
                (outcome, manager_id, resolution, now, case_id))
            self._connection.execute("""INSERT INTO audit
                (audit_id,actor,action,subject_type,subject_id,details,at) VALUES (?,?,?,?,?,?,?)""",
                (new_id(), 'manager:' + manager_id, 'handoff.' + outcome, 'handoff', case_id,
                 json.dumps({'message_id': case.message_id, 'resolution': resolution}), now))
            result = self.get_case(case_id)
            self._connection.commit()
            return result
        except Exception:
            self._connection.rollback()
            raise


_SELECT = ('SELECT h.*,m.content AS original_message FROM communication_handoff h '
           'JOIN message m ON m.message_id=h.message_id AND m.property_id=h.property_id')
