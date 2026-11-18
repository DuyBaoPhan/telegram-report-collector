"""Deterministic phase-6 offline resilience checks; no credentials or network."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetUpdates
from aiogram.types import Chat, Message, Update, User
from pydantic import ValidationError
import pytest

from app.database.database import Database
from app.parser import ReportParser, ReportProcessor


STAMP = datetime(2026, 11, 18, 2, tzinfo=timezone.utc)


def rules_file(path, groups=(-10,), users=(42,)):
    path.write_text(json.dumps({
        'labels': {'report_code': ['Code'], 'report_date': ['Date'],
                   'employee_name': ['Employee'], 'work_content': ['Work']},
        'required_fields': ['report_code', 'report_date', 'employee_name', 'work_content'],
        'multiline_fields': ['work_content'], 'date_formats': ['%Y-%m-%d'],
        'timezone': 'UTC', 'policies': {'employee_ids': list(users),
            'group_ids': list(groups), 'submission_hours': [0, 23]}}), encoding='utf-8')
    return path


def update(uid, chat=-10, mid=None, text='ordinary', edit=False, **extra):
    message = Message(message_id=mid or uid, date=STAMP,
        chat=Chat(id=chat, type='supergroup'),
        from_user=User(id=42, is_bot=False, first_name='Offline'), text=text, **extra)
    return Update(update_id=uid, **{'edited_message' if edit else 'message': message})


def test_concurrent_thousand_collect_replay_and_edit(tmp_path):
    db = Database(tmp_path/'offline.db')
    groups = (-10, -20, -30, -40, -50)
    for chat in groups:
        db.register(chat, str(chat))
    updates = [update(i, groups[i % 5]) for i in range(1, 1001)]
    # Independent SQLite connections contend on real transactions, not a mocked lock.
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert all(pool.map(db.collect, updates))
    restarted = Database(db.path)
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert not any(pool.map(restarted.collect, updates))
    assert restarted.stats() == {'messages': 1000, 'revisions': 1000, 'groups': 5}
    original = updates[0].message
    assert restarted.collect(update(2001, original.chat.id, original.message_id, 'newest', True))
    assert restarted.collect(update(1500, original.chat.id, original.message_id, 'stale', True))
    with restarted.connect() as conn:
        assert conn.execute('SELECT content FROM raw_messages WHERE chat_id=? AND message_id=?',
            (original.chat.id, original.message_id)).fetchone()[0] == 'newest'
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert Database(db.path).stats()['revisions'] == 1002


def test_media_and_malformed_metadata_are_offline(tmp_path):
    db = Database(tmp_path/'offline.db'); db.register(-10, 'Media')
    processor = ReportProcessor(db, ReportParser(rules_file(tmp_path/'rules.json')))
    # File IDs are inert metadata: no Bot is constructed, no download is possible.
    examples = [
        update(1, text=None, photo=[{'file_id': 'inert', 'file_unique_id': 'p', 'width': 1, 'height': 1}]),
        update(2, text=None, document={'file_id': 'inert', 'file_unique_id': 'd',
            'file_name': '../../=malformed.xlsx', 'mime_type': 'not/a-real-format'}),
        update(3, text=None, caption='Code: MEDIA\nDate: 2026-11-18\nEmployee: A\nWork: Caption',
            document={'file_id': 'inert', 'file_unique_id': 'c'}),
    ]
    for item in examples:
        assert db.collect(item)
        processor.process(db.raw_id_for_update(item.update_id))
    with pytest.raises(ValidationError):
        update(4, text=None, document={'file_name': 'missing-required-file-ids'})
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_messages WHERE processing_status='PENDING'").fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM reports').fetchone()[0] == 1
        assert conn.execute('SELECT status FROM reports').fetchone()[0] == 'VALID'
        assert conn.execute('SELECT COUNT(*) FROM raw_messages').fetchone()[0] == 3


@pytest.mark.parametrize('database_failure', [False, True])
def test_polling_retries_offsets_and_cleanup_without_network(tmp_path, monkeypatch, database_failure):
    module = importlib.import_module('app.bot.bot')
    db = Database(tmp_path/'offline.db'); db.register(-10, 'Offline')
    rules = rules_file(tmp_path/'rules.json')
    settings = SimpleNamespace(schedule_path=tmp_path/'schedule.json', admin_ids={99},
        database_path=db.path, test_group_id=-1, report_rules_path=rules, bot_token='not-used')
    settings.schedule_path.write_text('{"enabled": false}')
    item = update(71)
    bot = SimpleNamespace(get_webhook_info=AsyncMock(return_value=SimpleNamespace(url='')),
        get_me=AsyncMock(return_value=SimpleNamespace(username='offline')),
        session=SimpleNamespace(close=AsyncMock()))
    method = GetUpdates()
    bot.get_updates = AsyncMock(side_effect=[
        TelegramNetworkError(method=method, message='offline synthetic'),
        TelegramRetryAfter(method=method, message='offline synthetic', retry_after=2),
        [item], asyncio.CancelledError()])
    schedule = SimpleNamespace(start=Mock(), stop=AsyncMock(), notify_changes=AsyncMock())
    dispatcher = SimpleNamespace(feed_update=AsyncMock())
    sleep = AsyncMock()
    monkeypatch.setattr(module, 'Bot', lambda token: bot)
    monkeypatch.setattr(module, 'Database', lambda path: db)
    monkeypatch.setattr(module, 'ScheduleService', lambda *a: schedule)
    monkeypatch.setattr(module, 'create_dispatcher', lambda *a: dispatcher)
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    if database_failure:
        monkeypatch.setattr(db, 'collect', Mock(side_effect=OSError('synthetic disk failure')))
    async def scenario():
        with pytest.raises(RuntimeError if database_failure else asyncio.CancelledError):
            await module.run_bot(settings)
    asyncio.run(scenario())
    assert [call.args[0] for call in sleep.await_args_list] == [5, 2]
    offsets = [call.kwargs['offset'] for call in bot.get_updates.await_args_list]
    assert offsets == ([None, None, None] if database_failure else [None, None, None, 72])
    assert dispatcher.feed_update.await_count == (0 if database_failure else 1)
    assert db.stats()['messages'] == (0 if database_failure else 1)
    schedule.stop.assert_awaited_once(); bot.session.close.assert_awaited_once()
