import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import AsyncMock
import asyncio
import pytest
from aiogram.types import Update, Message, Chat, User
from app.parser import ReportParser, ReportProcessor
from app.database.database import Database
from app.config import Settings
from app.bot.bot import create_dispatcher

RULES = Path(__file__).resolve().parents[1] / 'config' / 'report_rules.json'
TEXT = 'Báo cáo\nMã báo cáo: BC-01\nNgày báo cáo: 18/11/2026\nNhân viên: An\nNội dung: Kiểm tra\nDòng hai'


def update(uid=1, mid=1, text=TEXT, edit=False):
    msg = Message(message_id=mid, date=datetime(2026,11,18,2,tzinfo=timezone.utc),
        chat=Chat(id=-10, type='supergroup'), from_user=User(id=42,is_bot=False,first_name='An'),text=text)
    return Update(update_id=uid, **{'edited_message' if edit else 'message': msg})


@pytest.fixture(autouse=True)
def isolated_rules(tmp_path, monkeypatch):
    import sys
    c = json.loads(RULES.read_text(encoding='utf-8-sig'))
    c['policies'] = {'employee_ids': None, 'group_ids': None, 'submission_hours': None}
    path = tmp_path / 'isolated_base_rules.json'
    path.write_text(json.dumps(c, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(sys.modules[__name__], 'RULES', path)


@pytest.fixture
def parser(tmp_path):
    c = json.loads(RULES.read_text(encoding='utf-8-sig'))
    c['policies'] = {'employee_ids':[42], 'group_ids':[-10], 'submission_hours':[8,18]}
    path = tmp_path/'rules.json'
    path.write_text(json.dumps(c,ensure_ascii=False),encoding='utf-8')
    return ReportParser(path)


@pytest.fixture
def service(tmp_path, parser):
    db = Database(tmp_path/'test.db')
    db.register(-10, 'Test')
    return db, ReportProcessor(db, parser)


def parse(parser,text):
    return parser.parse(text,user_id=42,chat_id=-10,submitted_at='2026-11-18T02:00:00+00:00')


def rows(db):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM reports ORDER BY raw_message_id')]


def ingest(db, proc, u):
    db.collect(u)
    return proc.process(db.raw_id_for_update(u.update_id))


def test_complete_unicode_multiline_aliases(parser):
    r=parse(parser,TEXT)
    assert r.status=='VALID'
    assert r.fields['report_date']=='2026-11-18'
    assert r.fields['work_content']=='Kiểm tra\nDòng hai'
    assert parse(parser,TEXT.replace('Mã báo cáo','MA BAO CAO')).status=='VALID'


@pytest.mark.parametrize('text,issue', [
    (TEXT.replace('18/11/2026','31/02/2026'),'invalid_date:report_date'),
    (TEXT.replace('Mã báo cáo: BC-01\n',''),'missing:report_code'),
    (TEXT.replace('Nhân viên: An','Nhân viên: '),'missing:employee_name'),
    ('Báo cáo','missing:report_code'),
])
def test_invalid_missing_no_guess(parser,text,issue):
    r=parse(parser,text)
    assert r.status=='INVALID'
    assert issue in r.issues


def test_chat_ignored_and_conflicting_label_review(parser):
    assert parse(parser,'Xin chào mọi người').status is None
    assert parse(parser,None).status is None
    assert parse(parser,'Ngày: 18/11/2026').status is None
    r=parse(parser,TEXT+'\nMã BC: BC-02')
    assert r.status=='NEED_REVIEW'
    assert r.fields['report_code']=='BC-01'


def test_unconfigured_and_disallowed_policies(parser):
    r=parse(ReportParser(RULES),TEXT)
    assert r.status=='NEED_REVIEW'
    assert len(r.issues)==3
    assert parser.parse(TEXT,user_id=99,chat_id=-99,submitted_at='2026-11-18T15:00:00+00:00').status=='INVALID'
    assert parser.parse(TEXT,user_id=42,chat_id=-10,submitted_at=None).status=='NEED_REVIEW'


def test_persist_replay_and_idempotence(service):
    db,proc=service
    db.collect(update())
    assert proc.replay_pending()=={'processed':1,'failed':0}
    assert proc.replay_pending()=={'processed':0,'failed':0}
    assert rows(db)[0]['status']=='VALID'
    assert db.report_today('2026-11-18')==[{'status':'VALID','count':1}]
    with db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM report_history').fetchone()[0]==1
    assert not db.collect(update())


def test_edit_duplicate_transitions_and_audit(service):
    db,proc=service
    ingest(db,proc,update())
    ingest(db,proc,update(2,2,TEXT.replace('BC-01','bc-01')))
    assert [r['status'] for r in rows(db)]==['VALID','DUPLICATE']
    assert rows(db)[1]['duplicate_of']==1
    ingest(db,proc,update(3,1,TEXT.replace('BC-01','BC-03'),True))
    assert [r['status'] for r in rows(db)]==['VALID','VALID']
    ingest(db,proc,update(4,1,TEXT.replace('BC-01','bc-01'),True))
    assert [r['status'] for r in rows(db)]==['VALID','DUPLICATE']
    ingest(db,proc,update(5,1,'Xin chào',True))
    assert [r['status'] for r in rows(db)]==['VALID']
    with db.connect() as conn:
        revisions=conn.execute('SELECT message_json FROM message_revisions WHERE raw_message_id=1 ORDER BY update_id').fetchall()
        assert len(revisions)==4
        assert 'BC-01' in revisions[0][0]
        assert conn.execute("SELECT COUNT(*) FROM report_history WHERE status='DUPLICATE'").fetchone()[0]>=2
        assert conn.execute('SELECT processing_status FROM raw_messages WHERE id=1').fetchone()[0]=='IGNORED'


def test_invalid_does_not_reserve_code_and_owner_invalidated(service):
    db,proc=service
    ingest(db,proc,update())
    ingest(db,proc,update(2,2))
    ingest(db,proc,update(3,1,TEXT.replace('18/11/2026','bad'),True))
    assert [r['status'] for r in rows(db)]==['INVALID','VALID']
    ingest(db,proc,update(4,1,TEXT,True))
    assert [r['status'] for r in rows(db)]==['VALID','DUPLICATE']


def test_failure_rollback_raw_retained_and_recovery(service):
    db,proc=service
    ingest(db,proc,update())
    db.collect(update(2,1,TEXT.replace('BC-01','BC-02'),True))
    with db.connect() as conn:
        conn.execute("CREATE TRIGGER fail_history BEFORE INSERT ON report_history BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        proc.process(1)
    assert rows(db)[0]['report_code']=='BC-01'  # entire processing transaction rolled back
    with db.connect() as conn:
        raw=conn.execute('SELECT * FROM raw_messages').fetchone()
        assert raw['processing_status']=='PENDING'
        assert raw['processing_error']=='IntegrityError'
        assert 'BC-02' in raw['content']
        assert conn.execute('SELECT COUNT(*) FROM message_revisions').fetchone()[0]==2
        conn.execute('DROP TRIGGER fail_history')
    assert proc.replay_pending()=={'processed':1,'failed':0}
    assert rows(db)[0]['report_code']=='BC-02'


def test_parser_exception_pending(service,monkeypatch):
    db,proc=service
    db.collect(update())
    monkeypatch.setattr(proc.parser,'parse',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('sensitive content')))
    assert proc.replay_pending()=={'processed':0,'failed':1}
    assert db.errors()[0]['processing_error']=='RuntimeError'
    assert rows(db)==[]


def test_reprocess_keeps_raw_and_versions(service):
    db,proc=service
    ingest(db,proc,update())
    proc.parser=ReportParser(RULES)
    assert proc.reprocess(1)=={'queued':1,'processed':1,'failed':0}
    assert rows(db)[0]['status']=='NEED_REVIEW'
    assert db.stats()['revisions']==1
    assert proc.reprocess(999)=={'queued':0,'processed':0,'failed':0}
    with db.connect() as conn:
        assert conn.execute('SELECT COUNT(DISTINCT rules_hash) FROM report_history').fetchone()[0]==2


def test_additive_legacy_migration(tmp_path):
    path=tmp_path/'old.db'
    db=Database(path)
    db.register(-10,'Legacy')
    db.collect(update())
    with db.connect() as conn:
        conn.execute('DROP TABLE report_history')
        conn.execute('DROP TABLE reports')
        conn.execute('PRAGMA user_version=1')
    reopened=Database(path)
    assert reopened.stats()=={'messages':1,'revisions':1,'groups':1}
    with reopened.connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==7


def test_private_admin_only_commands(service):
    db,proc=service
    settings=Settings('123:fake',frozenset({42}),-1)
    dp=create_dispatcher(settings,db,proc)
    handler=dp.message.handlers[0].callback
    async def check():
        for user,kind in ((99,'private'),(42,'supergroup')):
            m=AsyncMock()
            m.from_user=User(id=user,is_bot=False,first_name='Test')
            m.chat=Chat(id=-10 if kind=='supergroup' else user,type=kind)
            m.text='/reprocess all'
            await handler(m)
            m.answer.assert_not_called()
        m=AsyncMock()
        m.from_user=User(id=42,is_bot=False,first_name='Admin')
        m.chat=Chat(id=42,type='private')
        for command in ('/report_today','/errors','/reprocess','/reprocess all'):
            m.text=command
            await handler(m)
        assert m.answer.await_count==4
    asyncio.run(check())

@pytest.mark.parametrize('change', [
    {'required_fields':['unknown']},
    {'date_formats':['%d/%m']},
    {'policies':{'employee_ids':['42']}},
    {'policies':{'submission_hours':[25,2]}},
    {'labels':{'report_code':['Mã'], 'report_date':['Mã']}},
])
def test_bad_rules_rejected(tmp_path,change):
    c=json.loads(RULES.read_text(encoding='utf-8-sig'))
    c.update(change)
    path=tmp_path/'bad.json'
    path.write_text(json.dumps(c),encoding='utf-8')
    with pytest.raises(ValueError):
        ReportParser(path)


def test_ambiguous_date_review(tmp_path):
    c=json.loads(RULES.read_text(encoding='utf-8-sig'))
    c['date_formats']=['%d/%m/%Y','%m/%d/%Y']
    path=tmp_path/'ambiguous.json'
    path.write_text(json.dumps(c),encoding='utf-8')
    result=parse(ReportParser(path),TEXT.replace('18/11/2026','01/02/2026'))
    assert 'ambiguous_date:report_date' in result.issues
    assert result.status=='NEED_REVIEW'


def test_polling_ack_after_raw_commit_and_processing_failure(tmp_path,monkeypatch,parser):
    import app.bot.bot as module
    path=tmp_path/'polling.db'
    db=Database(path)
    db.register(-10,'Test')
    db.collect(update(90,90))  # startup replay
    offsets=[]
    class FakeBot:
        def __init__(self,*a):
            self.session=AsyncMock()
        async def get_webhook_info(self):
            with db.connect() as conn:
                assert conn.execute('SELECT processing_status FROM raw_messages').fetchone()[0]=='PROCESSED'
            return type('Webhook',(),{'url':''})()
        async def get_me(self):
            return type('Me',(),{'username':'offline_test'})()
        async def get_updates(self,offset,**kw):
            offsets.append(offset)
            if len(offsets)==1:
                return [update(100,1),update(101,2)]
            raise RuntimeError('offline stop')
    original=ReportParser.parse
    def faulty_parse(self,text,**kw):
        with db.connect() as conn:
            count=conn.execute('SELECT COUNT(*) FROM raw_messages').fetchone()[0]
        if count==2:
            raise ValueError('test failure')
        return original(self,text,**kw)
    monkeypatch.setattr(module,'Bot',FakeBot)
    monkeypatch.setattr(ReportParser,'parse',faulty_parse)
    dispatcher=AsyncMock()
    async def feed(bot,u):
        assert db.raw_id_for_update(u.update_id) is not None
    dispatcher.feed_update.side_effect=feed
    monkeypatch.setattr(module,'create_dispatcher',lambda *a:dispatcher)
    settings=Settings('123:fake',frozenset({42}),-10,database_path=path,report_rules_path=RULES)
    with pytest.raises(RuntimeError,match='offline stop'):
        asyncio.run(module.run_bot(settings))
    assert offsets==[None,102]
    assert db.stats()['messages']==3
    with db.connect() as conn:
        assert [r[0] for r in conn.execute('SELECT processing_status FROM raw_messages ORDER BY id')]==['PROCESSED','PENDING','PROCESSED']
    assert dispatcher.feed_update.await_count==2
