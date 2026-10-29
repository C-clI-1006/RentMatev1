"""Background lifecycle and durable outbox recovery, with real SQLite/auth."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from evidence.dispatch import EvidenceDispatcher, parse_managers
from main import create_app
from llm.classify import StubDocumentClassifier
from maintenance.outbox import RetryableHandoffError
from test_evidence_maintenance import integrated, counts
from test_maintenance import setup, Assessor


def queued(store, event_id):
    with store.transaction() as db:
        return db.execute("SELECT status FROM mc_outbox WHERE event_id=? AND target='evidence'",
                          (event_id,)).fetchone()[0]


def test_background_closure_to_http_timeline_without_manual_dispatch(integrated, monkeypatch):
    store, _, _, evidence, rid, doc, _ = integrated
    import maintenance.api
    monkeypatch.setattr(maintenance.api, 'LlmPriorityAssessor', Assessor)
    monkeypatch.setenv('RENTMATE_AUTH_TOKENS', json.dumps({
        'worker-manager': {'tenant_id':'manager-session','manager_id':'m1','property_id':'p1'},
        'worker-tenant': {'tenant_id':'t1','property_id':'p1'},
    }))
    app = create_app(store.path, evidence._storage_root, classifier=StubDocumentClassifier(),
                     evidence_dispatch_managers={'p1':'m1'}, evidence_dispatch_interval=0.05)
    manager = {'Authorization':'Bearer worker-manager'}
    try:
        with TestClient(app) as c:
            for status, version in [('approved',2), ('in_progress',3),
                                    ('awaiting_confirmation',4), ('closed',5)]:
                response = c.post(f'/api/maintenance/requests/{rid}/status', headers=manager,
                                  json={'target_status':status, 'expected_version':version,
                                        'reason':'Synthetic integration test','evidence_refs':[doc]})
                assert response.status_code == 200, response.text
                assert response.json()['status'] == 'applied'
            deadline = time.monotonic() + 8
            while True:
                result = c.get('/api/evidence/maintenance-timeline', headers=manager,
                               params={'request_id':rid,'property_id':'p1'})
                assert result.status_code == 200
                timeline = result.json()
                if any(row['event']['to_status'] == 'closed' for row in timeline):
                    break
                assert time.monotonic() < deadline, timeline
                time.sleep(0.02)
            closed = next(row for row in timeline if row['event']['to_status'] == 'closed')
            assert closed['event']['evidence_refs'] == [doc]
            assert closed['received_by'] == 'm1'
            receipt = c.get('/api/evidence/maintenance-events/' + closed['event']['event_id'],
                            headers=manager, params={'property_id':'p1'})
            assert receipt.status_code == 200 and receipt.json()['status'] == 'accepted'
            document = c.get('/api/evidence/documents/' + doc,
                             headers={'Authorization':'Bearer worker-tenant'}, params={'property_id':'p1'})
            assert document.json()['association'] == 'associated'
        assert not app.state.evidence_dispatcher._thread.is_alive()
        before = counts(store)
        app.state.evidence_dispatcher.run_once()
        assert counts(store) == before
    finally:
        app.state.evidence_service._repo.close()


@pytest.mark.parametrize('commit_first', [False, True])
def test_timeout_is_reconciled_before_retry(integrated, monkeypatch, commit_first):
    store, _, _, service, _, _, payload = integrated
    worker = EvidenceDispatcher(store.path, service, {'p1':'m1'})
    receive = service.receive_maintenance_event
    calls = []
    def timeout(event, auth):
        calls.append(event['event_id'])
        if commit_first:
            receive(event, auth)
        raise TimeoutError('Synthetic ambiguous result')
    monkeypatch.setattr(service, 'receive_maintenance_event', timeout)
    result = worker.run_once()
    assert result and all(row['status'] == 'uncertain' for row in result)
    event_id = calls[0]
    assert queued(store, event_id) == 'uncertain'
    monkeypatch.setattr(service, 'receive_maintenance_event', receive)
    worker.run_once()
    assert queued(store, event_id) == 'handed_off'
    assert counts(store) == (2,2,0)


def test_processing_absence_is_not_blindly_retried(integrated):
    store, _, _, service, _, _, payload = integrated
    with store.transaction() as db:
        db.execute("UPDATE mc_outbox SET status='processing' WHERE target='evidence'")
    assert EvidenceDispatcher(store.path, service, {'p1':'m1'}).run_once() == []
    assert counts(store) == (0,0,0)
    with store.transaction() as db:
        assert {row[0] for row in db.execute("SELECT status FROM mc_outbox WHERE target='evidence'")} == {'processing'}


def test_restart_does_not_treat_unknown_timeout_absence_as_safe_retry(integrated):
    store, _, _, service, _, _, _ = integrated
    with store.transaction() as db:
        db.execute("UPDATE mc_outbox SET status='uncertain' WHERE target='evidence'")
    assert EvidenceDispatcher(store.path, service, {'p1':'m1'}).run_once() == []
    assert counts(store) == (0,0,0)
    with store.transaction() as db:
        assert {row[0] for row in db.execute("SELECT status FROM mc_outbox WHERE target='evidence'")} == {'uncertain'}


def test_processing_committed_receipt_recovers_and_conflicts_reject(integrated):
    store, _, manager, service, _, _, payload = integrated
    # Persist exactly the original outbox payload, then simulate loss of acknowledgement.
    with store.transaction() as db:
        row = db.execute("SELECT event_id,document FROM mc_outbox WHERE target='evidence' LIMIT 1").fetchone()
    original = json.loads(row['document'])
    service.receive_maintenance_event(original, manager.auth)
    with store.transaction() as db:
        db.execute("UPDATE mc_outbox SET status='processing' WHERE event_id=? AND target='evidence'", (row['event_id'],))
    worker = EvidenceDispatcher(store.path, service, {'p1':'m1'})
    worker.run_once()
    assert queued(store, row['event_id']) == 'handed_off'
    original['reason'] = 'Different content with the same ID'
    with store.transaction() as db:
        db.execute("UPDATE mc_outbox SET status='uncertain',document=? WHERE event_id=? AND target='evidence'",
                   (json.dumps(original),row['event_id']))
    before = counts(store)
    worker.run_once()
    assert queued(store, row['event_id']) == 'rejected'
    assert counts(store) == before


def test_worker_requires_configured_binding_and_ignores_actor(integrated):
    store, _, _, service, _, _, _ = integrated
    assert EvidenceDispatcher(store.path, service, {'p1':'m2'}).run_once() == []
    assert counts(store) == (0,0,0)
    worker = EvidenceDispatcher(store.path, service, {'p1':'m1'})
    assert all(row['status'] == 'handed_off' for row in worker.run_once())
    with store.transaction() as db:
        assert {row[0] for row in db.execute('SELECT received_by FROM evidence_event_receipt')} == {'m1'}
        assert {row[0] for row in db.execute("SELECT status FROM mc_outbox WHERE target='communication'")} == {'pending'}


def test_permanent_bad_reference_is_rejected_without_receipt(integrated):
    store, _, _, service, _, _, _ = integrated
    with store.transaction() as db:
        row = db.execute("SELECT event_id,document FROM mc_outbox WHERE target='evidence' LIMIT 1").fetchone()
        payload = json.loads(row['document'])
        payload['evidence_refs'] = ['missing-document']
        db.execute("UPDATE mc_outbox SET document=? WHERE event_id=? AND target='evidence'",
                   (json.dumps(payload),row['event_id']))
    results = EvidenceDispatcher(store.path, service, {'p1':'m1'}).run_once()
    assert next(result['status'] for result in results if result['event_id'] == row['event_id']) == 'rejected'
    with store.transaction() as db:
        assert db.execute('SELECT 1 FROM evidence_event_receipt WHERE event_id=?', (row['event_id'],)).fetchone() is None
        assert db.execute("SELECT last_error FROM mc_outbox WHERE event_id=? AND target='evidence'",
                          (row['event_id'],)).fetchone()[0] == 'evidence_not_found'


def test_definite_nonacceptance_retries_next_cycle(integrated, monkeypatch):
    store, _, _, service, _, _, _ = integrated
    worker = EvidenceDispatcher(store.path, service, {'p1':'m1'})
    receive = service.receive_maintenance_event
    def busy(payload, auth):
        raise RetryableHandoffError('Synthetic confirmed rollback')
    monkeypatch.setattr(service, 'receive_maintenance_event', busy)
    assert worker.run_once()[0]['status'] == 'retry'
    assert counts(store) == (0,0,0)
    monkeypatch.setattr(service, 'receive_maintenance_event', receive)
    assert all(row['status'] == 'handed_off' for row in worker.run_once())
    assert counts(store) == (2,2,0)


@pytest.mark.parametrize('raw', ['[]','null','{"p1":null}','{"":"m1"}','not-json'])
def test_invalid_worker_configuration_is_rejected(raw):
    with pytest.raises(ValueError, match='must map property IDs'):
        parse_managers(raw)
