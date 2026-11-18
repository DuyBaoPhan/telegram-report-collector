"""Review issues 2/5: offline accepted revision and durable admin attempts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import Chat, MessageEntity, Update

from app.bot.bot import create_dispatcher, CommandClaimError
from app.database.database import Database
from test_phase6 import update


def command(uid=1, mid=1, text='/schedule_run', private=True, user=42):
    msg = update(uid, mid=mid, text=text).message.model_copy(update={
        'chat': Chat(id=user if private else -10, type='private' if private else 'supergroup'),
        'entities': [MessageEntity(type='bot_command', offset=0, length=len(text.split()[0]))]})
    msg = msg.model_copy(update={'from_user': msg.from_user.model_copy(update={'id': user})})
    return Update(update_id=uid, message=msg)


def dispatch(db, event, schedule=None):
    async def run():
        bot = Bot('123456:abcdefghijklmnopqrstuvwxyz123456789')
        bot.session.make_request = AsyncMock(return_value=True)
        try:
            dp = create_dispatcher(SimpleNamespace(admin_ids={42}), db, schedule=schedule)
            await dp.feed_update(bot, event)
        finally:
            await bot.session.close()
    asyncio.run(run())


def test_edit_tie_after_reseed_ignores_historical_high_ids(tmp_path):
    db = Database(tmp_path/'db'); db.register(-10, 'group')
    original = update(1000, mid=1, text='original')
    first = update(10, mid=1, text='first', edit=True, edit_date=1794967300)
    stale = update(9000, mid=1, text='old audit', edit=True, edit_date=1794967299)
    second = update(11, mid=1, text='second', edit=True, edit_date=1794967300)
    for event in (original, first, stale, second):
        assert db.collect(event)
        assert not db.collect(event)
    with db.connect() as conn:
        row = conn.execute('SELECT m.content,r.update_id FROM raw_messages m JOIN message_revisions r ON r.id=m.current_revision_id').fetchone()
        assert tuple(row) == ('second', 11)
        assert conn.execute('SELECT COUNT(*) FROM message_revisions').fetchone()[0] == 4


def test_crash_claim_restart_replay_and_fresh_identity(tmp_path):
    path = tmp_path/'db'; db = Database(path)
    event = command()
    key = db.event_fingerprint(event.update_id, False, event.message.model_dump_json(exclude_none=True))
    assert db.claim_command(key, '/schedule_run')
    db = Database(path)  # simulate restart after claim, before/after unknown external effect
    schedule = SimpleNamespace(run=AsyncMock(return_value='SUCCESS'))
    dispatch(db, event, schedule)
    schedule.run.assert_not_awaited()
    with db.connect() as conn:
        assert conn.execute('SELECT status FROM admin_command_attempts').fetchone()[0] == 'UNCERTAIN'
    dispatch(db, command(mid=2), schedule)  # same update_id, distinct real event
    dispatch(db, command(mid=2), schedule)
    schedule.run.assert_awaited_once_with(manual=True)


def test_registration_replay_cannot_reactivate_disabled_group(tmp_path):
    db = Database(tmp_path/'db'); event = command(text='/register_group private-label', private=False)
    dispatch(db, event); db.disable(-10)
    dispatch(Database(tmp_path/'db'), event)
    assert not db.groups()[0]['active']
    with db.connect() as conn:
        ledger = dict(conn.execute('SELECT * FROM admin_command_attempts').fetchone())
        assert ledger['status'] == 'SUCCESS'
        assert 'private-label' not in str(ledger)
        assert 'message_json' not in ledger


def test_unauthorized_normal_and_edits_do_not_claim(tmp_path):
    db = Database(tmp_path/'db')
    schedule = SimpleNamespace(run=AsyncMock())
    dispatch(db, command(user=99), schedule)
    dispatch(db, update(2, text='ordinary report'), schedule)
    event = command(3)
    dispatch(db, Update(update_id=3, edited_message=event.message), schedule)
    schedule.run.assert_not_awaited()
    with db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM admin_command_attempts').fetchone()[0] == 0


def test_failed_attempt_and_finalize_failure_are_never_retried(tmp_path, monkeypatch):
    db = Database(tmp_path/'db'); schedule = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError('private failure')))
    with pytest.raises(RuntimeError):
        dispatch(db, command(), schedule)
    dispatch(db, command(), schedule)
    schedule.run.assert_awaited_once()
    with db.connect() as conn:
        assert conn.execute('SELECT status FROM admin_command_attempts').fetchone()[0] == 'FAILED'
    monkeypatch.setattr(db, 'finish_command', lambda *args: (_ for _ in ()).throw(OSError('disk')))
    schedule.run = AsyncMock(return_value='SUCCESS')
    dispatch(db, command(mid=2), schedule); dispatch(db, command(mid=2), schedule)
    schedule.run.assert_awaited_once()


def test_claim_failure_has_no_effect_and_can_retry_after_repair(tmp_path, monkeypatch):
    db = Database(tmp_path/'db'); schedule = SimpleNamespace(run=AsyncMock(return_value='SUCCESS'))
    original = db.claim_command
    monkeypatch.setattr(db, 'claim_command', lambda *args: (_ for _ in ()).throw(OSError('private failure')))
    with pytest.raises(CommandClaimError, match='persist'):
        dispatch(db, command(), schedule)
    schedule.run.assert_not_awaited()
    monkeypatch.setattr(db, 'claim_command', original)
    dispatch(db, command(), schedule)
    schedule.run.assert_awaited_once()


def test_polling_claim_failure_stops_before_next_ack(tmp_path, monkeypatch):
    import importlib
    from unittest.mock import Mock
    from test_phase6 import rules_file
    module = importlib.import_module('app.bot.bot')
    db = Database(tmp_path/'db')
    settings = SimpleNamespace(schedule_path=tmp_path/'schedule.json', admin_ids={42},
        database_path=db.path, test_group_id=-1, report_rules_path=rules_file(tmp_path/'rules'), bot_token='unused')
    settings.schedule_path.write_text('{"enabled": false}')
    bot = SimpleNamespace(get_webhook_info=AsyncMock(return_value=SimpleNamespace(url='')),
        get_me=AsyncMock(return_value=SimpleNamespace(username='offline')),
        session=SimpleNamespace(close=AsyncMock()), get_updates=AsyncMock(return_value=[command()]))
    schedule = SimpleNamespace(start=Mock(), stop=AsyncMock(), notify_changes=AsyncMock())
    dispatcher = SimpleNamespace(feed_update=AsyncMock(side_effect=CommandClaimError('claim failed')))
    monkeypatch.setattr(module, 'Bot', lambda token: bot)
    monkeypatch.setattr(module, 'Database', lambda path: db)
    monkeypatch.setattr(module, 'ScheduleService', lambda *args: schedule)
    monkeypatch.setattr(module, 'create_dispatcher', lambda *args: dispatcher)
    with pytest.raises(CommandClaimError):
        asyncio.run(module.run_bot(settings))
    assert [call.kwargs['offset'] for call in bot.get_updates.await_args_list] == [None]
    schedule.stop.assert_awaited_once(); bot.session.close.assert_awaited_once()


def test_schema6_additive_backfill_preserves_payload_and_history(tmp_path):
    db = Database(tmp_path/'db'); db.register(-10, 'group')
    db.collect(update(1000, mid=1)); db.collect(update(10, mid=1, text='accepted', edit=True, edit_date=1794967300))
    with db.connect() as conn:
        old_raw = tuple(conn.execute('SELECT id,message_json FROM raw_messages').fetchone())
        old_revisions = [tuple(r) for r in conn.execute('SELECT * FROM message_revisions')]
        conn.execute('ALTER TABLE raw_messages DROP COLUMN current_revision_id')
        conn.execute('DROP TABLE admin_command_attempts')
        conn.execute('PRAGMA user_version=6')
    reopened = Database(tmp_path/'db')
    with reopened.connect() as conn:
        assert tuple(conn.execute('SELECT id,message_json FROM raw_messages').fetchone()) == old_raw
        assert [tuple(r) for r in conn.execute('SELECT * FROM message_revisions')] == old_revisions
        assert conn.execute('SELECT r.update_id FROM raw_messages m JOIN message_revisions r ON r.id=m.current_revision_id').fetchone()[0] == 10
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 7
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
