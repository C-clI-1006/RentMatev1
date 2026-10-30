"""Real SQLite/HTTP regressions for stale reminders and human work items."""

import json
import sqlite3
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

from contracts.drafting import ReviewDecision
from lease_rent.service import run_due_alerts
from main import create_app
from notification import dispatch_one, SQLiteNotificationInboxAdapter
from review.service import ReviewService
from review.handoffs import record_handoff
from scripts.seed_integrated_demo import seed
from shared import AuthContext
from shared.sqlite import init_connection, SCHEMA_VERSION

NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / 'gaps.db'
    seed(path)
    db = init_connection(path)
    db.execute("UPDATE lease SET start_date='2026-01-01',end_date='2026-11-05' WHERE lease_id='demo-lease'")
    db.execute("UPDATE rent_record SET due_date='2026-10-01',amount_due=2000,amount_paid=0 WHERE rent_record_id='demo-rent'")
    db.commit()
    monkeypatch.setattr('notification.dispatcher.utcnow', lambda: NOW)
    yield path, db
    db.close()


def approve_reminder(path, db, kind):
    run_due_alerts(now=NOW, db_path=path)
    draft = db.execute('SELECT * FROM draft WHERE source_kind=?', (kind,)).fetchone()
    final = 'Manager reviewed exact wording: ' + draft['content']
    ReviewService(db, AuthContext('demo-tenant','demo-property','demo-manager')).decide(
        draft['draft_id'], expected_version=1, decision=ReviewDecision.APPROVED, final_content=final)
    return f"draft:{draft['draft_id']}:v1", draft['draft_id'], final


@pytest.mark.parametrize('kind,change', [
    ('rent_reminder', "UPDATE rent_record SET amount_paid=amount_due,status='paid' WHERE rent_record_id='demo-rent'"),
    ('rent_reminder', "UPDATE rent_record SET amount_paid=100 WHERE rent_record_id='demo-rent'"),
    ('rent_reminder', "UPDATE rent_record SET status='paid' WHERE rent_record_id='demo-rent'"),
    ('rent_reminder', "UPDATE rent_record SET due_date='2026-11-01' WHERE rent_record_id='demo-rent'"),
    ('rent_reminder', "UPDATE rent_record SET amount_due='invalid' WHERE rent_record_id='demo-rent'"),
    ('rent_reminder', "DELETE FROM rent_record WHERE rent_record_id='demo-rent'"),
    ('rent_reminder', "UPDATE lease SET end_date='2026-10-20' WHERE lease_id='demo-lease'"),
    ('rent_reminder', "UPDATE lease SET version='renewed' WHERE lease_id='demo-lease'"),
    ('rent_reminder', "UPDATE lease SET property_id='demo-property-2' WHERE lease_id='demo-lease'"),
    ('lease_deadline', "UPDATE lease SET end_date='2027-11-05' WHERE lease_id='demo-lease'"),
    ('lease_deadline', "UPDATE lease SET start_date='2026-10-26' WHERE lease_id='demo-lease'"),
    ('lease_deadline', "UPDATE lease SET document_ref='changed-terms' WHERE lease_id='demo-lease'"),
    ('lease_deadline', "UPDATE lease SET tenant_id='demo-tenant-2' WHERE lease_id='demo-lease'"),
])
def test_changed_reminder_is_suppressed_and_audited(database, kind, change):
    path, db = database
    event, draft_id, final = approve_reminder(path, db, kind)
    db.execute(change); db.commit()
    first = dispatch_one(db, event, 'demo-tenant', SQLiteNotificationInboxAdapter(db))
    second = dispatch_one(db, event, 'demo-tenant', SQLiteNotificationInboxAdapter(db))
    assert first.status.value == second.status.value == 'failed'
    assert first.attempts == second.attempts == 1
    assert first.detail.startswith('reminder_')
    assert db.execute('SELECT COUNT(*) FROM tenant_inbox').fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM audit WHERE event_id=? AND action='delivery.failed'", (event,)).fetchone()[0] == 1
    assert db.execute('SELECT final_content FROM review WHERE draft_id=?', (draft_id,)).fetchone()[0] == final
    assert json.loads(db.execute('SELECT payload FROM notification_event WHERE event_id=?', (event,)).fetchone()[0])['content'] == final


@pytest.mark.parametrize('kind', ['rent_reminder', 'lease_deadline'])
def test_unchanged_reminder_delivers_exact_approved_text_once(database, kind):
    path, db = database
    event, _, final = approve_reminder(path, db, kind)
    first = dispatch_one(db, event, 'demo-tenant', SQLiteNotificationInboxAdapter(db))
    second = dispatch_one(db, event, 'demo-tenant', SQLiteNotificationInboxAdapter(db))
    assert first.status.value == second.status.value == 'delivered'
    assert first.attempts == second.attempts == 1
    assert db.execute('SELECT content FROM tenant_inbox WHERE event_id=?', (event,)).fetchone()[0] == final


def test_expired_lease_is_suppressed_without_source_edits(database, monkeypatch):
    path, db = database
    event, _, _ = approve_reminder(path, db, 'lease_deadline')
    monkeypatch.setattr('notification.dispatcher.utcnow', lambda: NOW + timedelta(days=20))
    result = dispatch_one(db, event, 'demo-tenant', SQLiteNotificationInboxAdapter(db))
    assert result.status.value == 'failed'
    assert result.detail == 'reminder_lease_inactive'


def test_missing_legacy_basis_fails_closed(database):
    path, db = database
    event, draft, _ = approve_reminder(path, db, 'rent_reminder')
    db.execute('DELETE FROM reminder_basis WHERE draft_id=?', (draft,)); db.commit()
    result = dispatch_one(db, event, 'demo-tenant', SQLiteNotificationInboxAdapter(db))
    assert result.status.value == 'failed'
    assert result.detail == 'reminder_basis_unverifiable'


def test_transient_reminder_fact_read_failure_recovers(database, monkeypatch):
    from lease_rent.repository import SQLiteLeaseRentRepository
    path, db = database
    event, _, final = approve_reminder(path, db, 'rent_reminder')
    original = SQLiteLeaseRentRepository.reminder_facts
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('private internal detail')
    monkeypatch.setattr(SQLiteLeaseRentRepository, 'reminder_facts', unavailable)
    first = dispatch_one(db, event, 'demo-tenant', SQLiteNotificationInboxAdapter(db))
    assert first.status.value == 'pending_retry'
    assert first.detail == 'reminder_facts_temporarily_unavailable'
    assert db.execute('SELECT COUNT(*) FROM tenant_inbox').fetchone()[0] == 0
    monkeypatch.setattr(SQLiteLeaseRentRepository, 'reminder_facts', original)
    second = dispatch_one(db, event, 'demo-tenant', SQLiteNotificationInboxAdapter(db))
    assert second.status.value == 'delivered' and second.attempts == 2
    assert db.execute('SELECT content FROM tenant_inbox WHERE event_id=?', (event,)).fetchone()[0] == final


@pytest.fixture
def client(database, tmp_path, monkeypatch):
    accounts = {
        'tenant': dict(tenant_id='demo-tenant', property_id='demo-property'),
        'manager': dict(tenant_id='demo-tenant', property_id='demo-property', manager_id='demo-manager'),
        'other': dict(tenant_id='demo-tenant-2', property_id='demo-property-2', manager_id='demo-manager-2'),
    }
    monkeypatch.setenv('RENTMATE_AUTH_TOKENS', json.dumps(accounts))
    app = create_app(str(database[0]), str(tmp_path / 'evidence'))
    try:
        with TestClient(app) as http:
            yield http
    finally:
        app.state.evidence_service._repo.close()


def auth(role):
    return {'Authorization': 'Bearer ' + role}


@pytest.mark.parametrize('classification', ['unknown','invalid','exception'])
def test_classification_handoff_visible_only_to_bound_manager(client, database, monkeypatch, classification):
    from llm.classifier import Classification, Category
    class Classifier:
        def classify(self, content):
            if classification == 'exception':
                raise RuntimeError('model failure')
            return Classification(Category.UNKNOWN) if classification == 'unknown' else None
    monkeypatch.setattr('communication.api.build_communication_classifier', Classifier)
    response = client.post('/api/communication/messages', headers=auth('tenant'), json={'content':'Unclear original message'})
    assert response.status_code == 200
    assert response.json()['reply_status'] == 'manual_handling'
    assert response.json()['delivery_status'] == 'not_dispatched'
    cases = client.get('/api/reviews/handoffs', headers=auth('manager')).json()
    assert len(cases) == 1
    case = cases[0]
    assert case['message_id'] == response.json()['message_id']
    assert case['original_message'] == 'Unclear original message'
    assert case['property_id'] == 'demo-property' and case['status'] == 'open'
    db = database[1]
    assert db.execute('SELECT COUNT(*) FROM draft WHERE message_id=?', (case['message_id'],)).fetchone()[0] == 0
    assert client.get('/api/reviews/handoffs', headers=auth('tenant')).status_code == 403
    assert client.get('/api/reviews/handoffs').status_code in (401,403)
    assert client.get('/api/reviews/handoffs', headers=auth('other')).json() == []
    path = '/api/reviews/handoffs/' + case['case_id']
    assert client.get(path, headers=auth('other')).status_code == 404
    body = {'outcome':'resolved', 'resolution':'Handled offline by manager'}
    assert client.post(path+'/resolution', headers=auth('other'), json=body).status_code == 404
    assert client.post(path+'/resolution', headers=auth('tenant'), json=body).status_code == 403


@pytest.mark.parametrize('outcome', ['resolved','dismissed'])
def test_handoff_resolution_is_durable_idempotent_and_does_not_send(client, database, outcome):
    response = client.post('/api/communication/messages', headers=auth('tenant'), json={'content':'xyzzy frobnicate'})
    case = client.get('/api/reviews/handoffs', headers=auth('manager')).json()[0]
    path = '/api/reviews/handoffs/' + case['case_id'] + '/resolution'
    body = {'outcome':outcome, 'resolution':'Manager handled the original request offline'}
    first = client.post(path, headers=auth('manager'), json=body)
    second = client.post(path, headers=auth('manager'), json=body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()['resolver_id'] == 'demo-manager' and first.json()['resolved_at']
    assert first.json()['status'] == outcome
    assert client.post(path, headers=auth('manager'), json={**body,'resolution':'Different decision'}).status_code == 409
    db = database[1]
    record_handoff(db, response.json()['message_id'], 'retry')
    assert db.execute('SELECT COUNT(*) FROM communication_handoff').fetchone()[0] == 1
    assert db.execute('SELECT status FROM communication_handoff').fetchone()[0] == outcome
    assert db.execute('SELECT COUNT(*) FROM notification_event').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM review').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM draft WHERE message_id=?', (response.json()['message_id'],)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM audit WHERE action=?", ('handoff.'+outcome,)).fetchone()[0] == 1
    reopened = init_connection(database[0])
    assert reopened.execute('SELECT status FROM communication_handoff').fetchone()[0] == outcome
    reopened.close()


def test_handoff_creation_and_resolution_roll_back_then_recover(client, database):
    db = database[1]
    db.execute("""INSERT INTO message(message_id,tenant_id,property_id,content,message_type,received_at)
        VALUES ('recovery','demo-tenant','demo-property','original','unknown',?)""", (NOW.isoformat(),));db.commit()
    db.execute("""CREATE TRIGGER fail_handoff BEFORE INSERT ON audit
        WHEN NEW.action='handoff.created' BEGIN SELECT RAISE(ABORT,'audit unavailable'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        record_handoff(db, 'recovery', 'classifier failed')
    assert db.execute('SELECT COUNT(*) FROM communication_handoff').fetchone()[0] == 0
    db.execute('DROP TRIGGER fail_handoff');db.commit()
    record_handoff(db, 'recovery', 'classifier failed')
    record_handoff(db, 'recovery', 'retry')
    case = client.get('/api/reviews/handoffs', headers=auth('manager')).json()[0]
    db.execute("""CREATE TRIGGER fail_resolution BEFORE INSERT ON audit
        WHEN NEW.action='handoff.resolved' BEGIN SELECT RAISE(ABORT,'audit unavailable'); END""")
    from review.handoffs import HandoffService
    service = HandoffService(db, AuthContext('demo-tenant','demo-property','demo-manager'))
    with pytest.raises(sqlite3.IntegrityError):
        service.settle(case['case_id'], outcome='resolved', resolution='Checked offline')
    assert service.get_case(case['case_id']).status == 'open'
    db.execute('DROP TRIGGER fail_resolution');db.commit()
    assert service.settle(case['case_id'], outcome='resolved', resolution='Checked offline').status == 'resolved'


def test_failed_handoff_creation_cannot_commit_an_orphan_message(database, client):
    db = database[1]
    before = db.execute('SELECT COUNT(*) FROM message').fetchone()[0]
    db.execute("""CREATE TRIGGER fail_creation BEFORE INSERT ON audit
        WHEN NEW.action='handoff.created' BEGIN SELECT RAISE(ABORT,'audit unavailable'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        client.post('/api/communication/messages', headers=auth('tenant'), json={'content':'xyzzy frobnicate'})
    assert db.execute('SELECT COUNT(*) FROM message').fetchone()[0] == before
    assert db.execute('SELECT COUNT(*) FROM communication_handoff').fetchone()[0] == 0
    db.execute('DROP TRIGGER fail_creation');db.commit()
    assert client.post('/api/communication/messages', headers=auth('tenant'), json={'content':'xyzzy frobnicate'}).status_code == 200
    assert len(client.get('/api/reviews/handoffs', headers=auth('manager')).json()) == 1


def test_revoked_manager_and_invalid_resolution_cannot_settle_handoff(client, database):
    client.post('/api/communication/messages', headers=auth('tenant'), json={'content':'xyzzy frobnicate'})
    case = client.get('/api/reviews/handoffs', headers=auth('manager')).json()[0]
    path = '/api/reviews/handoffs/' + case['case_id'] + '/resolution'
    for body in [{'outcome':'approved','resolution':'No'}, {'outcome':'resolved','resolution':'   '},
                 {'outcome':'resolved','resolution':'Done','manager_id':'demo-manager'}]:
        assert client.post(path, headers=auth('manager'), json=body).status_code == 422
    db = database[1]
    db.execute("DELETE FROM manager_property WHERE manager_id='demo-manager'");db.commit()
    assert client.get('/api/reviews/handoffs', headers=auth('manager')).status_code == 403
    assert client.post(path, headers=auth('manager'), json={'outcome':'resolved','resolution':'Done'}).status_code == 403
    assert db.execute('SELECT status FROM communication_handoff').fetchone()[0] == 'open'


def test_v10_upgrade_recovers_audited_handoff_without_fabricating_reminder_basis(database):
    path, db = database
    db.execute("""INSERT INTO message(message_id,tenant_id,property_id,content,message_type,received_at)
        VALUES ('old','demo-tenant','demo-property','old original','unknown',?)""", (NOW.isoformat(),))
    db.execute("""INSERT INTO audit(audit_id,actor,action,subject_type,subject_id,at)
        VALUES ('old-audit','system','message.classified_invalid','message','old',?)""", (NOW.isoformat(),))
    db.execute('DROP TABLE communication_handoff');db.execute('DROP TABLE reminder_basis')
    db.execute('PRAGMA user_version=10');db.commit()
    upgraded = init_connection(path)
    assert upgraded.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION == 11
    assert upgraded.execute('SELECT reason FROM communication_handoff').fetchone()[0] == 'legacy_handoff'
    assert upgraded.execute('SELECT COUNT(*) FROM reminder_basis').fetchone()[0] == 0
    upgraded.close()
    again = init_connection(path)
    assert again.execute('SELECT COUNT(*) FROM communication_handoff').fetchone()[0] == 1
    again.close()
