"""Independent manager ownership, role isolation and legacy upgrade regressions."""
import json
import os
import sqlite3

import pytest

from shared.sqlite import init_connection, _ensure_v10_manager_evidence
from test_evidence_extract import build_text_pdf
from test_evidence_upload import client, _upload, HEADERS_T1_P1


@pytest.fixture
def manager_client(client, monkeypatch):
    db = client.app.state.evidence_service._repo._conn
    db.execute("INSERT INTO manager VALUES ('M1','Manager','2026-10-28')")
    db.execute("INSERT INTO manager VALUES ('M2','Other','2026-10-28')")
    db.execute("INSERT INTO manager_property VALUES ('M1','P1','2026-10-28')")
    db.execute("INSERT INTO manager_property VALUES ('M2','P2','2026-10-28')")
    db.commit()
    accounts = json.loads(os.environ['RENTMATE_AUTH_TOKENS'])
    accounts.update({
        'manager-fixture': {'manager_id': 'M1', 'property_id': 'P1'},
        'other-manager-fixture': {'manager_id': 'M2', 'property_id': 'P2'},
        'unbound-fixture': {'manager_id': 'M2', 'tenant_id': 'T1', 'property_id': 'P1'},
    })
    monkeypatch.setenv('RENTMATE_AUTH_TOKENS', json.dumps(accounts))
    return client


def headers(token='manager-fixture'):
    return {'Authorization': 'Bearer ' + token}


def test_manager_without_tenant_upload_classify_review_and_read(manager_client):
    c = manager_client
    pdf = build_text_pdf(['Invoice for kitchen repair AUD 150'])
    response = _upload(c, 'invoice.pdf', pdf, headers(), 'P1')
    assert response.status_code == 200
    doc = response.json()['document']
    assert doc['tenant_id'] is None
    assert doc['uploaded_by_manager_id'] == 'M1'
    assert doc['classification'] == 'classified'
    doc_id = doc['document_id']
    base = '/api/evidence/documents/' + doc_id
    assert c.get(base, params={'property_id': 'P1'}, headers=headers()).json()['uploaded_by_manager_id'] == 'M1'
    assert doc_id in [d['document_id'] for d in c.get('/api/evidence/documents', params={'property_id':'P1'}, headers=headers()).json()]
    download = c.get(base + '/download', params={'property_id':'P1'}, headers=headers())
    assert download.status_code == 200 and download.content == pdf
    text = c.get(base + '/text', params={'property_id':'P1'}, headers=headers())
    assert text.status_code == 200 and 'AUD 150' in text.json()['text']
    reviewed = c.post('/api/evidence/reviews', headers=headers(), json={
        'status':'confirm_suggested', 'document_id':doc_id, 'property_id':'P1',
    })
    assert reviewed.status_code == 200
    assert reviewed.json()['reviewed_by'] == 'M1'
    # A resident of the same property must not inherit manager-owned documents.
    assert c.get(base, params={'property_id':'P1'}, headers=HEADERS_T1_P1).status_code == 404
    assert c.get('/api/evidence/documents', params={'property_id':'P1'}, headers=HEADERS_T1_P1).json() == []
    for suffix in ['', '/download', '/text']:
        assert c.get(base + suffix, params={'property_id':'P1'}, headers=headers('other-manager-fixture')).status_code == 403


@pytest.mark.parametrize('token,property_id', [
    ('manager-fixture','P2'), ('unbound-fixture','P1'),
])
def test_manager_denial_never_falls_back_to_tenant(manager_client, token, property_id):
    c = manager_client
    assert _upload(c, 'invoice.pdf', b'%PDF-test', headers(token), property_id).status_code == 403
    db = c.app.state.evidence_service._repo._conn
    assert db.execute('SELECT count(*) FROM evidence_document').fetchone()[0] == 0


def test_manager_invalid_upload_leaves_no_record(manager_client):
    response = _upload(manager_client, 'bad.exe', b'bad', headers(), 'P1')
    assert response.status_code == 200
    assert response.json()['status'] == 'rejected'
    assert manager_client.app.state.evidence_service._repo._conn.execute(
        'SELECT count(*) FROM evidence_document').fetchone()[0] == 0


def test_manager_reads_existing_tenant_document(manager_client):
    c = manager_client
    response = _upload(c, 'invoice.pdf', build_text_pdf(['Invoice']), HEADERS_T1_P1, 'P1')
    doc = response.json()['document']
    assert doc['tenant_id'] == 'T1' and doc['uploaded_by_manager_id'] is None
    result = c.get('/api/evidence/documents/' + doc['document_id'],
                   params={'property_id':'P1'}, headers=headers())
    assert result.status_code == 200 and result.json()['tenant_id'] == 'T1'


def test_failed_ownership_migration_rolls_back_and_restores_fk():
    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'ALTER TABLE evidence_document_v10 RENAME TO evidence_document':
                raise sqlite3.OperationalError('injected migration failure')
            return super().execute(sql, *args, **kwargs)

    db = sqlite3.connect(':memory:', factory=FailingConnection)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE evidence_document (document_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL)')
        db.execute("INSERT INTO evidence_document VALUES ('D','T')")
        db.execute('PRAGMA user_version=9')
        db.commit()
        with pytest.raises(sqlite3.OperationalError, match='injected'):
            _ensure_v10_manager_evidence(db)
        assert db.execute('SELECT * FROM evidence_document').fetchall() == [('D','T')]
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert db.execute('PRAGMA user_version').fetchone()[0] == 9
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='evidence_document_v10'").fetchall()
    finally:
        db.close()


def test_v9_upgrade_preserves_document_children_and_foreign_keys(tmp_path):
    path = tmp_path / 'legacy.db'
    db = init_connection(path)
    db.close()
    db = sqlite3.connect(path)
    db.execute('PRAGMA foreign_keys=OFF')
    db.execute('DROP TABLE evidence_document')
    db.execute('CREATE TABLE evidence_document (document_id TEXT PRIMARY KEY, '
               'tenant_id TEXT NOT NULL REFERENCES tenant(tenant_id), '
               'property_id TEXT NOT NULL REFERENCES property(property_id), '
               'original_filename TEXT NOT NULL, file_type TEXT NOT NULL, '
               'size_bytes INTEGER NOT NULL, storage_ref TEXT NOT NULL, '
               'upload_status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
    db.execute("INSERT INTO tenant VALUES ('T','Tenant','contact','now')")
    db.execute("INSERT INTO property VALUES ('P','Property','address','now')")
    db.execute("INSERT INTO evidence_document VALUES ('D','T','P','x.pdf','pdf',1,'evidence/x.pdf','saved','now','now')")
    db.execute("INSERT INTO evidence_classification (document_id,status,source,details,created_at) VALUES ('D','failed','none','{}','now')")
    db.execute("INSERT INTO evidence_review VALUES ('R','D','select_category','invoice','M','now')")
    db.execute('PRAGMA user_version=9')
    db.commit()
    db.close()
    for _ in range(2):
        db = init_connection(path)
        try:
            assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
            assert db.execute('PRAGMA foreign_key_check').fetchall() == []
            assert db.execute('SELECT tenant_id, uploaded_by_manager_id FROM evidence_document').fetchone()[:] == ('T', None)
            assert db.execute('SELECT final_category FROM evidence_review').fetchone()[0] == 'invoice'
            assert db.execute('SELECT status FROM evidence_classification').fetchone()[0] == 'failed'
        finally:
            db.close()
