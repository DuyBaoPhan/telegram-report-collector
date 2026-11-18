import base64
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.dashboard import DashboardSettings, create_app, read_database, runtime_status
from app.database.database import Database
from app.heartbeat import Heartbeat, heartbeat_path

PASSWORD = 'test-only-password-123'


def client(path):
    return TestClient(create_app(DashboardSettings(path, 'admin', PASSWORD)))


def auth():
    return {'Authorization': 'Basic '+base64.b64encode(('admin:'+PASSWORD).encode()).decode()}


def fixture_db(tmp_path):
    database = Database(tmp_path / 'fixture.db')
    database.register(-10, '<script>alert(1)</script>')
    database.register(-20, 'Inactive')
    with database.connect() as db:
        db.execute('UPDATE telegram_groups SET active=0 WHERE chat_id=-20')
        db.execute('''INSERT INTO raw_messages(id,chat_id,message_id,submitted_at,content,message_json,processing_error)
            VALUES (1,-10,42,?,'sensitive raw body','sensitive payload','sensitive error token')''',
            (datetime.now(timezone.utc).isoformat(),))
        db.execute('''INSERT INTO reports(raw_message_id,fields_json,base_status,status,issues_json,rules_hash)
            VALUES(1,'sensitive report','VALID','VALID','[]','hash')''')
        db.execute("UPDATE raw_messages SET processing_status='PROCESSED'")
        db.execute("INSERT INTO export_history(run_id,report_date,file_path,status,error) VALUES('test','2026-11-18','private/file.xlsx','FAILED','sensitive error')")
        db.execute("INSERT INTO scheduled_runs(slot_key,report_date,trigger_type,status,detail) VALUES('test','2026-11-18','MANUAL','FAILED','sensitive detail')")
    return database


@pytest.mark.parametrize('route',['/','/assets/dashboard.js','/assets/dashboard.css','/api/overview','/api/changes','/api/groups','/api/messages','/nonexistent'])
def test_auth_every_route(tmp_path,route):
    c=client(tmp_path/'none.db')
    assert c.get(route).status_code==401
    assert c.get(route,headers={'Authorization':'Basic !!!'}).status_code==401
    assert c.get(route,auth=('admin','incorrect-password')).status_code==401


def test_no_database_created_and_no_path_leak(tmp_path):
    path=tmp_path/'missing.db'
    c=client(path)
    result=c.get('/api/overview',headers=auth())
    assert result.status_code==503
    assert str(path) not in result.text
    assert not path.exists()
    assert c.get('/api/groups',headers=auth()).status_code==503
    assert c.get('/api/messages',headers=auth()).status_code==503


def test_overview_metadata_only_and_headers(tmp_path):
    database=fixture_db(tmp_path)
    c=client(database.path)
    response=c.get('/api/overview',headers=auth())
    assert response.status_code==200, response.text
    data=response.json()
    assert data['counts']['messages']==1
    assert data['counts']['today']==1
    assert data['counts']['groups']==1
    assert data['statuses']==[{'status':'VALID','count':1}]
    assert len(data['daily'])==7
    assert sum(day['count'] for day in data['daily'])==1
    assert data['runtime']['state']=='unknown'
    assert 'sensitive' not in response.text
    assert 'file_path' not in response.text
    assert 'no-store' in response.headers['cache-control']
    assert response.headers['x-frame-options']=='DENY'
    assert "script-src 'self'" in response.headers['content-security-policy']


def test_groups_filters_pagination_safe_search(tmp_path):
    database=fixture_db(tmp_path)
    c=client(database.path)
    data=c.get('/api/groups?active=active&limit=1',headers=auth()).json()
    assert data['total']==1
    assert data['items'][0]['messages']==1
    assert data['items'][0]['display_name']=='<script>alert(1)</script>'
    assert c.get('/api/groups',params={'q':"' OR 1=1 --"},headers=auth()).json()['total']==0
    assert c.get('/api/groups',params={'q':'%'},headers=auth()).json()['total']==0
    assert c.get('/api/groups?active=invalid',headers=auth()).status_code==422
    assert c.get('/api/groups?page=0',headers=auth()).status_code==422
    assert c.get('/api/groups?limit=9999',headers=auth()).status_code==422


def test_messages_never_expose_sensitive_payload(tmp_path):
    database=fixture_db(tmp_path)
    c=client(database.path)
    response=c.get('/api/messages?status=VALID',headers=auth())
    assert response.status_code==200
    item=response.json()['items'][0]
    assert item['has_error']==1
    assert item['message_id']==42
    assert not {'content','message_json','username','user_id','employee_name','processing_error','fields_json'} & item.keys()
    assert 'sensitive' not in response.text
    assert c.get('/api/messages?status=INVALID',headers=auth()).json()['total']==0
    assert c.get('/api/messages?status=bad',headers=auth()).status_code==422


def test_routes_only_read_and_static_allowlist(tmp_path):
    database=fixture_db(tmp_path)
    c=client(database.path)
    assert c.post('/api/groups',headers=auth()).status_code==405
    assert c.get('/assets/not-a-file',headers=auth()).status_code==404
    assert c.get('/docs',headers=auth()).status_code==404
    assert c.get('/',headers=auth()).status_code==200
    js=c.get('/assets/dashboard.js',headers=auth()).text
    assert 'innerHTML' not in js
    assert 'localStorage.setItem(\'collector-theme\'' in js
    with read_database(database.path) as db:
        assert db.execute('PRAGMA query_only').fetchone()[0]==1
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM telegram_groups")


def test_unsupported_and_incomplete_schema_safe(tmp_path):
    path=tmp_path/'old.db'
    sqlite3.connect(path).close()
    c=client(path)
    assert c.get('/api/overview',headers=auth()).json()['error']=='unsupported_schema'
    assert c.get('/api/groups',headers=auth()).status_code==503
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=7')
    assert c.get('/api/overview',headers=auth()).status_code==503


def test_credentials_required_and_env_isolated(monkeypatch,tmp_path):
    monkeypatch.delenv('DASHBOARD_PASSWORD',raising=False)
    monkeypatch.setenv('BOT_TOKEN','must-not-be-needed')
    with pytest.raises(ValueError,match='DASHBOARD_PASSWORD'):
        DashboardSettings.from_env()
    with pytest.raises(ValueError):
        DashboardSettings(tmp_path/'x','admin','short')
    with pytest.raises(ValueError):
        DashboardSettings(tmp_path/'x','admin',PASSWORD,80)
    monkeypatch.setenv('DASHBOARD_PASSWORD',PASSWORD)
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'not-created.db'))
    assert DashboardSettings.from_env().database_path==tmp_path/'not-created.db'


def test_heartbeat_states_atomic_and_stale(tmp_path):
    path=tmp_path/'test.db'
    assert runtime_status(path)['state']=='unknown'
    heartbeat=Heartbeat(path)
    heartbeat.write('starting')
    assert runtime_status(path)['state']=='starting'
    heartbeat.write('polling')
    data=runtime_status(path)
    assert data['state']=='polling'
    assert data['last_poll_at']
    heartbeat.write('retrying')
    assert runtime_status(path)['state']=='retrying'
    record=json.loads(heartbeat_path(path).read_text())
    record['updated_at']=(datetime.now(timezone.utc)-timedelta(seconds=100)).isoformat()
    heartbeat_path(path).write_text(json.dumps(record))
    assert runtime_status(path)['state']=='stale'
    heartbeat.write('stopped')
    assert runtime_status(path)['state']=='stopped'
    heartbeat_path(path).write_text('not json')
    assert runtime_status(path)['state']=='unknown'
    assert not list(tmp_path.glob('*.tmp'))


def test_heartbeat_write_failure_is_nonfatal(tmp_path):
    heartbeat=Heartbeat(tmp_path/'absent'/'test.db')
    heartbeat.write('polling')
    assert not heartbeat.path.exists()


def test_changes_missing_safe_and_metadata_only(tmp_path):
    path=tmp_path/'missing.db'
    response=client(path).get('/api/changes',headers=auth())
    assert response.status_code==503
    assert str(path) not in response.text
    assert not list(tmp_path.iterdir())


def test_changes_detect_wal_edits_status_and_heartbeat(tmp_path):
    database=fixture_db(tmp_path)
    c=client(database.path)
    def token():
        response=c.get('/api/changes',headers=auth())
        assert response.status_code==200
        data=response.json()
        assert list(data)==['token']
        assert len(data['token'])==64
        assert all(ch in '0123456789abcdef' for ch in data['token'])
        assert response.headers['cache-control']=='no-store'
        return data['token']
    with sqlite3.connect(database.path) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        before=token()
        assert token()==before
        statements=[
            "INSERT INTO raw_messages(chat_id,message_id,submitted_at,content,message_json) VALUES(-10,43,'2026-11-18','new','{}')",
            "UPDATE raw_messages SET content='edited' WHERE id=1",
            "UPDATE raw_messages SET processing_status='PENDING' WHERE id=1",
            "UPDATE reports SET status='INVALID' WHERE raw_message_id=1",
            "UPDATE export_history SET status='SUCCESS'",
            "UPDATE scheduled_runs SET status='SUCCESS'",
            "INSERT INTO realtime_dirty(report_date,chat_id,revision,completed_revision) VALUES('2026-11-18',-10,1,0)",
        ]
        for sql in statements:
            writer.execute(sql)
            writer.commit()
            after=token()
            assert after!=before,sql
            before=after
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before=token()
        Heartbeat(database.path).write('polling')
        assert token()!=before
        assert c.post('/api/changes',headers=auth()).status_code==405
