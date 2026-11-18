import asyncio
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User
from app.bot.bot import create_dispatcher, run_bot
from app.config import Settings
from app.database.database import Database
from app.excel import ExportGenerator
from app.scheduler import ScheduleConfig, ScheduleService


def config(tmp_path, **values):
    path = tmp_path / 'schedule.json'
    path.write_text(json.dumps(values), encoding='utf-8')
    return ScheduleConfig.load(path, {99, 100})


def service(tmp_path, **values):
    db = Database(tmp_path / 'test.db')
    db.register(-10, 'Test')
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    generator = SimpleNamespace(export=Mock(return_value=[{'status':'SUCCESS','path':tmp_path/'fake.xlsx'}]), templates=SimpleNamespace(mapping={}))
    clock = lambda: datetime(2026, 11, 18, 2, 0, 30, tzinfo=ZoneInfo('UTC'))
    return ScheduleService(db, bot, generator, config(tmp_path, **values), clock=clock)


def test_defaults_timezone_and_recipients(tmp_path):
    c = config(tmp_path)
    assert c.times == ('09:00','15:00','17:00')
    assert str(c.timezone) == 'Asia/Ho_Chi_Minh'
    assert c.recipients == frozenset({99,100})
    assert not c.enabled


@pytest.mark.parametrize('values', [
    {'recipient_admin_ids':[-10]}, {'recipient_admin_ids':[101]}, {'recipient_admin_ids':[]},
    {'recipient_admin_ids':[True]}, {'timezone':'Invalid/Zone'}, {'times':['24:00']},
    {'times':['9:00']}, {'group_times':{'1':['09:00']}}, {'group_times':{'-10':['09:99']}},
    {'enabled':'yes'}, {'typo':True},
])
def test_reject_unsafe_config(tmp_path, values):
    with pytest.raises(ValueError): config(tmp_path, **values)


def test_claim_survives_restart_and_overlap(tmp_path):
    s = service(tmp_path)
    async def scenario():
        results = await asyncio.gather(s.run(), s.run())
        assert sorted(results) == ['DUPLICATE','SUCCESS']
        restarted = ScheduleService(s.database, s.bot, s.generator, s.config, clock=s.clock)
        assert await restarted.run() == 'DUPLICATE'
        assert await restarted.run(manual=True) == 'SUCCESS'
        assert await restarted.run(manual=True) == 'SUCCESS'
    asyncio.run(scenario())
    assert s.generator.export.call_count == 3
    assert {c.args[0] for c in s.bot.send_document.call_args_list} == {99,100}
    assert len(s.history()) == 3
    assert 'UNKNOWN' in s.history()[0]['detail']


def test_failures_not_retried_and_sanitized(tmp_path):
    s = service(tmp_path)
    s.bot.send_document.side_effect = OSError('secret token')
    assert asyncio.run(s.run()) == 'PARTIAL'
    assert asyncio.run(s.run()) == 'DUPLICATE'
    assert s.bot.send_document.call_count == 2
    assert 'secret' not in s.history()[0]['detail']
    s.generator.export.side_effect = ValueError('secret')
    assert asyncio.run(s.run(manual=True)) == 'FAILED'
    assert s.history()[0]['detail'] == 'ValueError'


def test_roster_missing_only_valid_submissions(tmp_path):
    s = service(tmp_path)
    s.generator.templates.mapping['roster_by_chat_id'] = {'-10':[42,43]}
    with s.database.connect() as c:
        c.execute("INSERT INTO raw_messages(chat_id,message_id,user_id,submitted_at,message_json,processing_status) VALUES(-10,1,42,'2026-11-18','{}','PROCESSED')")
        c.execute("INSERT INTO reports(raw_message_id,report_date,fields_json,base_status,status,issues_json,rules_hash) VALUES(1,'2026-11-18','{}','VALID','VALID','[]','test')")
    assert s.missing('2026-11-18',[-10]) == '-10: missing 1 (43)'
    with s.database.connect() as c: c.execute("UPDATE raw_messages SET processing_status='PENDING'")
    assert 'missing 2 (42, 43)' in s.missing('2026-11-18',[-10])
    s.database.disable(-10)
    assert asyncio.run(s.run(-10)) == 'SKIPPED'
    s.generator.export.assert_not_called()


def test_schedule_jobs_timezone_and_lifecycle(tmp_path):
    s = service(tmp_path, enabled=True, group_times={'-10':['08:30']})
    async def scenario():
        s.start()
        jobs = s.scheduler.get_jobs()
        assert len(jobs) == 4
        for job in jobs:
            assert str(job.trigger.timezone) == 'Asia/Ho_Chi_Minh'
            assert job.coalesce and job.max_instances == 1 and job.misfire_grace_time == 60
        await s.stop()
        assert not s.scheduler.running
    asyncio.run(scenario())


def test_delayed_slot_uses_scheduled_minute(tmp_path):
    s = service(tmp_path)
    assert asyncio.run(s.run(slot_time='09:00')) == 'SUCCESS'
    assert s.history()[0]['slot_key'].endswith('2026-11-18T09:00')
    assert asyncio.run(s.run(slot_time='09:00')) == 'DUPLICATE'


def test_group_export_scope(tmp_path):
    db = Database(tmp_path/'test.db')
    db.register(-10,'A'); db.register(-11,'B')
    results = ExportGenerator(db,tmp_path/'out').export('2026-11-18',group_ids=[-10])
    assert len(results) == 2
    with db.connect() as c:
        assert {r[0] for r in c.execute('SELECT chat_id FROM export_history')} == {None,-10}


def test_private_admin_commands_only(tmp_path):
    s = service(tmp_path)
    settings = Settings('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi',frozenset({99}),-1)
    async def scenario():
        bot = Bot(settings.bot_token)
        bot.session = SimpleNamespace(__call__=AsyncMock())
        # Handler direct calls avoid any Telegram I/O while preserving real command routing.
        handler = create_dispatcher(settings,s.database,schedule=s).message.handlers[0].callback
        for user, kind in [(101,'private'),(99,'supergroup')]:
            m = SimpleNamespace(from_user=SimpleNamespace(id=user),text='/schedule_run',chat=SimpleNamespace(type=kind),answer=AsyncMock())
            await handler(m)
        s.generator.export.assert_not_called()
        m = SimpleNamespace(from_user=SimpleNamespace(id=99),text='/schedule_run',chat=SimpleNamespace(type='private'),answer=AsyncMock())
        await handler(m)
        assert s.generator.export.call_count == 1
        m.text='/schedule_status'
        await handler(m)
        assert 'SUCCESS' in m.answer.call_args.args[0]
    asyncio.run(scenario())


def test_run_bot_closes_scheduler_and_session_on_exception(tmp_path, monkeypatch):
    import app.bot.bot as module
    path = tmp_path/'schedule.json'; path.write_text('{}')
    settings = Settings('unused',frozenset({99}),-1,database_path=tmp_path/'db.sqlite',schedule_path=path)
    bot = SimpleNamespace(get_webhook_info=AsyncMock(return_value=SimpleNamespace(url='')),get_me=AsyncMock(return_value=SimpleNamespace(username='test')),get_updates=AsyncMock(side_effect=RuntimeError('stop')),session=SimpleNamespace(close=AsyncMock()))
    schedule = SimpleNamespace(start=Mock(),stop=AsyncMock())
    monkeypatch.setattr(module,'Bot',lambda token:bot)
    monkeypatch.setattr(module,'ScheduleService',lambda *args:schedule)
    with pytest.raises(RuntimeError): asyncio.run(run_bot(settings))
    schedule.start.assert_called_once(); schedule.stop.assert_awaited_once(); bot.session.close.assert_awaited_once()

def test_stop_drains_inflight_job(tmp_path):
    s = service(tmp_path, enabled=True)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        original = s.run
        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        s.run = blocked
        outer = asyncio.create_task(s.scheduled())
        await entered.wait()
        stopping = asyncio.create_task(s.stop())
        await asyncio.sleep(0)
        assert not stopping.done()
        release.set()
        await stopping
        await outer
        assert s.history()[0]['status'] == 'SUCCESS'
    asyncio.run(scenario())


def test_additive_schedule_migration_preserves_claim(tmp_path):
    s = service(tmp_path)
    assert s.claim('existing','2026-11-18',None,'SCHEDULED')
    reopened = Database(s.database.path)
    assert reopened.groups()[0]['chat_id'] == -10
    with reopened.connect() as c:
        assert c.execute('PRAGMA user_version').fetchone()[0] == 7
        assert c.execute('SELECT status FROM scheduled_runs').fetchone()[0] == 'CLAIMED'
    assert not s.claim('existing','2026-11-18',None,'SCHEDULED')


def test_export_missing_share_ingestion_lock_but_network_does_not(tmp_path):
    """Queue ingestion at the exact export/missing boundary, without sleeps."""
    import threading
    from openpyxl import load_workbook
    s = service(tmp_path)
    s.generator = ExportGenerator(s.database, tmp_path/'out')
    s.generator.templates.mapping['roster_by_chat_id'] = {'-10': [42]}
    original_export, original_missing = s.generator.export, s.missing
    attempting = threading.Event()
    exported = []
    observations = []

    async def scenario():
        loop = asyncio.get_running_loop()
        ingested = asyncio.Event()
        tasks = []

        async def ingest():
            attempting.set()
            async with s.lock:
                with s.database.connect() as conn:
                    conn.execute("INSERT INTO raw_messages(chat_id,message_id,user_id,submitted_at,message_json,processing_status) VALUES(-10,1,42,'2026-11-18','{}','PROCESSED')")
                    conn.execute("INSERT INTO reports(raw_message_id,report_date,fields_json,base_status,status,issues_json,rules_hash) VALUES(1,'2026-11-18','{}','VALID','VALID','[]','test')")
                ingested.set()

        def export(*args):
            results = original_export(*args)
            exported.extend(results)
            loop.call_soon_threadsafe(lambda: tasks.append(asyncio.create_task(ingest())))
            assert attempting.wait(5), 'ingestion did not reach shared lock'
            return results

        def missing(*args):
            observations.append((s.lock.locked(), ingested.is_set()))
            return original_missing(*args)

        async def network(*args):
            assert not s.lock.locked(), 'network must not block ingestion'
            await asyncio.wait_for(ingested.wait(), 5)

        s.generator.export = export
        s.missing = missing
        s.bot.send_document.side_effect = network
        s.bot.send_message.side_effect = network
        assert await asyncio.wait_for(s.run(), 10) == 'SUCCESS'
        await asyncio.gather(*tasks)
        assert observations == [(True, False)]
        assert original_missing('2026-11-18', [-10]) == '-10: missing 0'
        assert all('-10: missing 1 (42)' in call.args[1] for call in s.bot.send_message.await_args_list)
        for result in exported:
            book = load_workbook(result['path'])
            assert book['Reports']['A5'].value is None
            book.close()
        assert not s.lock.locked()

    asyncio.run(scenario())


def test_company_roster_still_includes_disabled_groups_notification_is_active_only(tmp_path):
    from openpyxl import load_workbook
    s = service(tmp_path)
    s.database.register(-20, 'Disabled')
    s.database.disable(-20)
    s.generator = ExportGenerator(s.database, tmp_path/'out')
    s.generator.templates.mapping['roster_by_chat_id'] = {'-10': [42], '-20': [43]}
    assert asyncio.run(s.run()) == 'SUCCESS'
    paths = list((tmp_path/'out').rglob('*.xlsx'))
    assert len(paths) == 2  # company plus active group, not a disabled-group workbook
    for path in paths:
        book = load_workbook(path)
        expected = 2 if book['Summary']['B4'].value == 'Company' else 1
        assert book['Summary']['B10'].value == expected
        book.close()
    assert all('-10: missing 1 (42)' in call.args[1] and '-20:' not in call.args[1]
               for call in s.bot.send_message.await_args_list)
