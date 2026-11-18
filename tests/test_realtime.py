"""Offline realtime integration tests: no Telegram, no production database, no sleeps."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiogram.types import Update, Message, Chat, User
from openpyxl import load_workbook
import pytest

from app.database.database import Database
from app.parser import ReportParser, ReportProcessor
from app.scheduler import ScheduleConfig, ScheduleService
from app.excel import ExportGenerator

DAY = '2026-11-18'
TEXT = 'Ma bao cao: A\nNgay bao cao: 18/11/2026\nNhan vien: An\nNoi dung: work'


def setup(tmp_path):
    rules = json.loads(Path('config/report_rules.json').read_text(encoding='utf-8-sig'))
    rules['policies'] = {'employee_ids':[42], 'group_ids':[-10,-20], 'submission_hours':[0,23]}
    path = tmp_path/'rules.json'; path.write_text(json.dumps(rules), encoding='utf-8')
    db = Database(tmp_path/'test.db')
    db.register(-10, 'One'); db.register(-20, 'Two')
    return db, ReportProcessor(db, ReportParser(path))


def ingest(db, processor, uid, text=TEXT, mid=1, chat=-10, edit=False):
    msg = Message(message_id=mid, date=datetime(2026,11,18,2,tzinfo=timezone.utc),
        chat=Chat(id=chat,type='supergroup'),from_user=User(id=42,is_bot=False,first_name='An'),text=text)
    update = Update(update_id=uid, **{'edited_message' if edit else 'message':msg})
    db.collect(update); processor.process(db.raw_id_for_update(uid))


def service(tmp_path, db):
    path=tmp_path/'schedule.json'
    path.write_text(json.dumps({'enabled':False,'recipient_admin_ids':[99],
        'realtime':{'enabled':True,'debounce_seconds':10}}))
    generator = SimpleNamespace(export=Mock(return_value=[{'status':'SUCCESS','path':tmp_path/'fake.xlsx'}]))
    return ScheduleService(db, SimpleNamespace(send_document=AsyncMock()), generator, ScheduleConfig.load(path,{99}))


def dirty(db):
    with db.connect() as c:
        return {(r['report_date'],r['chat_id']):r['revision'] for r in c.execute('SELECT * FROM realtime_dirty')}


def test_projection_new_noop_date_invalid_and_promotion(tmp_path):
    db,p=setup(tmp_path)
    ingest(db,p,1,'ordinary chat'); assert not dirty(db)
    ingest(db,p,2,mid=2); assert dirty(db)=={(DAY,-10):1}
    ingest(db,p,3,mid=2,edit=True); assert dirty(db)=={(DAY,-10):1}
    ingest(db,p,4,TEXT.replace('18/11','19/11'),mid=2,edit=True)
    assert dirty(db)=={(DAY,-10):2,('2026-11-19',-10):1}
    ingest(db,p,5,mid=3,chat=-20)  # duplicate, no projection change
    assert (DAY,-20) not in dirty(db)
    ingest(db,p,6,'ordinary chat',mid=2,edit=True)  # remove owner and promote duplicate
    assert dirty(db)[(DAY,-20)]==1
    assert dirty(db)[('2026-11-19',-10)]==2
    ingest(db,p,7,TEXT.replace('18/11/2026','bad'),mid=3,chat=-20,edit=True)
    assert dirty(db)[(DAY,-20)]==2


def test_dirty_changes_rollback_with_report(tmp_path):
    db,p=setup(tmp_path)
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_dirty BEFORE INSERT ON realtime_dirty BEGIN SELECT RAISE(ABORT,'fail'); END")
    with pytest.raises(Exception): ingest(db,p,1)
    assert not dirty(db)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM reports').fetchone()[0]==0
        c.execute('DROP TRIGGER fail_dirty')
    p.replay_pending(); assert dirty(db)[(DAY,-10)]==1


def test_burst_debounce_noop_and_restart(tmp_path):
    db,p=setup(tmp_path);s=service(tmp_path,db)
    async def scenario():
        ingest(db,p,1);await s.notify_changes(); first=s.debounce_handle
        ingest(db,p,2,'ordinary',mid=2);await s.notify_changes(); assert s.debounce_handle is first
        ingest(db,p,3,TEXT.replace('work','changed'),edit=True);await s.notify_changes()
        assert first.cancelled() and s.debounce_handle is not first
        await s.stop();assert s.pending()
        restarted=service(tmp_path,Database(db.path));restarted.start()
        await asyncio.gather(*list(restarted.tasks))
        assert restarted.debounce_handle is not None
        await restarted.flush_realtime();assert restarted.generator.export.call_count==1
        assert restarted.generator.export.call_args.args==(DAY,None,[-10])
        assert restarted.bot.send_document.await_count==1
        assert restarted.bot.send_document.call_args.args[0]==99
        assert not restarted.pending()
        await restarted.stop()
    asyncio.run(scenario())


def test_export_failure_retained_and_send_failure_not_retried(tmp_path):
    db,p=setup(tmp_path);ingest(db,p,1);s=service(tmp_path,db)
    async def scenario():
        s.generator.export.side_effect=PermissionError('locked')
        await s.flush_realtime();assert s.pending();assert not s.bot.send_document.called
        s.generator.export.side_effect=None
        s.bot.send_document.side_effect=TimeoutError('uncertain')
        await s.flush_realtime();assert not s.pending()
        with db.connect() as c:
            assert c.execute('SELECT status FROM realtime_attempts').fetchone()[0]=='PARTIAL'
        restarted=service(tmp_path,Database(db.path));await restarted.flush_realtime()
        assert not restarted.bot.send_document.called
    asyncio.run(scenario())


def test_new_revision_during_export_not_acknowledged(tmp_path):
    db,p=setup(tmp_path);ingest(db,p,1);s=service(tmp_path,db)
    def export(*args):
        ingest(db,p,2,TEXT.replace('work','changed'),edit=True)
        return [{'status':'SUCCESS','path':tmp_path/'fake.xlsx'}]
    s.generator.export.side_effect=export
    asyncio.run(s.flush_realtime())
    assert s.pending()[0]['revision']==2 and s.pending()[0]['completed_revision']==1


def test_company_full_snapshot_affected_group_only(tmp_path):
    db,p=setup(tmp_path)
    ingest(db,p,1);ingest(db,p,2,TEXT.replace(': A',': B'),chat=-20,mid=2)
    result=ExportGenerator(db,tmp_path/'out').export(DAY,None,[-10])
    assert len(result)==2 and all(r['status']=='SUCCESS' for r in result)
    company=load_workbook(result[0]['path']); group=load_workbook(result[1]['path'])
    try:
        assert company['Reports']['A5'].value is not None
        assert company['Reports']['A6'].value is not None
        assert group['Reports']['A5'].value is not None
        assert group['Reports']['A6'].value is None
    finally:
        company.close();group.close()


@pytest.mark.parametrize('value',[{'enabled':'yes'},{'debounce_seconds':0},{'debounce_seconds':True},{'debounce_seconds':float('nan')},{'unknown':1}])
def test_realtime_config_rejected(tmp_path,value):
    path=tmp_path/'schedule.json';path.write_text(json.dumps({'realtime':value}))
    with pytest.raises(ValueError):ScheduleConfig.load(path,{99})


def test_failed_pending_edit_defers_company_snapshot(tmp_path):
    db,p=setup(tmp_path);ingest(db,p,1);ingest(db,p,2,TEXT.replace(': A',': B'),mid=2,chat=-20)
    s=service(tmp_path,db)
    # Simulate a collected edit whose processor has not succeeded yet.
    with db.connect() as c:
        c.execute("UPDATE raw_messages SET processing_status='PENDING' WHERE message_id=1")
    asyncio.run(s.flush_realtime())
    assert s.pending() and not s.generator.export.called
    p.replay_pending()
    asyncio.run(s.flush_realtime())
    assert not s.pending() and s.generator.export.call_count==1


def test_unchanged_pending_resolution_rearms_debounce(tmp_path):
    db, p = setup(tmp_path)
    ingest(db, p, 1)
    ingest(db, p, 2, TEXT.replace(': A', ': B'), mid=2, chat=-20)
    s = service(tmp_path, db)

    async def scenario():
        await s.notify_changes()
        original_signature = s.pending_signature
        revisions_before = dirty(db)
        with db.connect() as c:
            c.execute("UPDATE raw_messages SET processing_status='PENDING' WHERE message_id=1")
        await s.flush_realtime()
        assert s.pending() and not s.generator.export.called
        assert s.pending_signature is None
        assert s.debounce_handle is None
        assert not s.tasks  # no immediate retry loop while processing is unresolved

        p.replay_pending()  # succeeds but does not change the exported projection
        assert dirty(db) == revisions_before
        await s.notify_changes()  # same hook as run_bot after /reprocess
        assert s.pending_signature == original_signature
        timer = s.debounce_handle
        assert timer is not None and not timer.cancelled()
        await s.notify_changes()  # ordinary traffic must not postpone the timer
        assert s.debounce_handle is timer
        await s.flush_realtime()
        assert s.generator.export.call_count == 1
        assert s.bot.send_document.await_count == 1
        assert not s.pending()
        await s.notify_changes()
        assert s.debounce_handle is None
        await s.stop()

    asyncio.run(scenario())
