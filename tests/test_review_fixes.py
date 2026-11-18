"""Offline review regressions: collision identity, atomic v5 migration, server retry."""
import asyncio
import importlib
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramServerError, TelegramUnauthorizedError, TelegramConflictError
from aiogram.methods import GetUpdates

from app.database.database import Database
from app.parser import ReportParser, ReportProcessor
from test_phase6 import update, rules_file


def test_colliding_ids_are_distinct_but_exact_replay_is_not(tmp_path):
    db = Database(tmp_path/'db'); db.register(-10, 'one'); db.register(-20, 'two')
    proc = ReportProcessor(db, ReportParser(rules_file(tmp_path/'rules', groups=(-10, -20))))
    a = update(7, -10, 1, 'original')
    b = update(7, -20, 2, 'other')
    c = update(7, -10, 1, 'edited', True, edit_date=1794967300)
    d = update(7, -10, 1, 'edited again', True, edit_date=1794967301)
    for item in (a, b, c, d):
        assert db.collect(item)
        assert proc.process(db.raw_id_for_update(item))
        assert not db.collect(item)
    assert db.raw_id_for_update(7) is None
    assert db.raw_id_for_update(a) != db.raw_id_for_update(b)
    assert db.raw_id_for_update(a) == db.raw_id_for_update(c)
    with db.connect() as conn:
        rows = conn.execute('SELECT * FROM message_revisions ORDER BY id').fetchall()
        assert len(rows) == 4
        assert [json.loads(r['message_json'])['text'] for r in rows] == ['original', 'other', 'edited', 'edited again']
        assert len({r['id'] for r in rows}) == 4
        assert conn.execute('SELECT content FROM raw_messages WHERE id=?', (db.raw_id_for_update(c),)).fetchone()[0] == 'edited again'
        assert [r[0] for r in conn.execute('SELECT source_revision_id FROM report_history ORDER BY id')] == [r['id'] for r in rows]
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def v5_fixture(path):
    # Build the actual previous schema from the compatibility initialization DDL.
    import inspect
    source = inspect.getsource(Database.__init__)
    schema = source.split('schema = """', 1)[1].split('"""', 1)[0]
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
        conn.execute('PRAGMA user_version=5')
        conn.execute("INSERT INTO telegram_groups(chat_id,display_name) VALUES (-10,'old')")
        payload = update(77, mid=1).message.model_dump_json(exclude_none=True)
        conn.execute("INSERT INTO raw_messages(id,chat_id,message_id,submitted_at,message_json) VALUES (1,-10,1,'old',?)", (payload,))
        conn.execute('INSERT INTO message_revisions(update_id,raw_message_id,is_edit,message_json) VALUES (77,1,0,?)', (payload,))
        conn.execute("INSERT INTO reports(raw_message_id,source_revision_id,fields_json,base_status,status,issues_json,rules_hash) VALUES (1,77,'{}','VALID','VALID','[]','old')")
        conn.execute("INSERT INTO report_history(raw_message_id,source_revision_id,status,fields_json,issues_json,rules_hash) VALUES (1,77,'VALID','{}','[]','old')")
    return payload


def test_migrate_v5_preserves_payloads_references_and_replay(tmp_path):
    path = tmp_path/'db'; payload = v5_fixture(path)
    db = Database(path)
    with db.connect() as conn:
        revision = conn.execute('SELECT * FROM message_revisions').fetchone()
        assert revision['id'] == revision['update_id'] == 77
        assert revision['message_json'] == payload
        for table in ('reports', 'report_history'):
            assert conn.execute(f'SELECT source_revision_id FROM {table}').fetchone()[0] == 77
            assert any(r['table'] == 'message_revisions' and r['to'] == 'id' for r in conn.execute(f'PRAGMA foreign_key_list({table})'))
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 7
    assert not db.collect(update(77, mid=1))
    assert db.collect(update(77, mid=2))
    assert Database(path).stats()['revisions'] == 2


def test_migration_failure_rolls_back_ddl_data_and_version(tmp_path, monkeypatch):
    path = tmp_path/'db'; v5_fixture(path)
    original = Database._migrate_revisions.__func__
    def fail(cls, conn):
        original(cls, conn)
        raise RuntimeError('injected migration failure')
    monkeypatch.setattr(Database, '_migrate_revisions', classmethod(fail))
    with pytest.raises(RuntimeError, match='injected'):
        Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 5
        assert [r[1] for r in conn.execute('PRAGMA table_info(message_revisions)')][0] == 'update_id'
        assert conn.execute('SELECT source_revision_id FROM reports').fetchone()[0] == 77
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%v6'").fetchall()
    monkeypatch.setattr(Database, '_migrate_revisions', classmethod(original))
    assert Database(path).stats()['revisions'] == 1


@pytest.mark.parametrize('fatal', [None, TelegramUnauthorizedError, TelegramConflictError])
def test_server_retry_backoff_retains_offset_and_fatal_errors_stop(tmp_path, monkeypatch, caplog, fatal):
    module = importlib.import_module('app.bot.bot')
    db = Database(tmp_path/'db'); db.register(-10, 'test')
    settings = SimpleNamespace(schedule_path=tmp_path/'schedule', admin_ids={99}, database_path=db.path,
        test_group_id=-1, report_rules_path=rules_file(tmp_path/'rules'), bot_token='not-used')
    settings.schedule_path.write_text('{"enabled": false}')
    method = GetUpdates()
    error = lambda: TelegramServerError(method=method, message='secret-url-token-and-payload')
    terminal = fatal(method=method, message='secret-url-token-and-payload') if fatal else asyncio.CancelledError()
    bot = SimpleNamespace(get_webhook_info=AsyncMock(return_value=SimpleNamespace(url='')),
        get_me=AsyncMock(return_value=SimpleNamespace(username='offline')), session=SimpleNamespace(close=AsyncMock()),
        get_updates=AsyncMock(side_effect=[[update(71)], *[error() for _ in range(6)], [update(72)], error(), [], terminal]))
    schedule = SimpleNamespace(start=Mock(), stop=AsyncMock(), notify_changes=AsyncMock())
    sleep = AsyncMock()
    monkeypatch.setattr(module, 'Bot', lambda token: bot)
    monkeypatch.setattr(module, 'Database', lambda path: db)
    monkeypatch.setattr(module, 'ScheduleService', lambda *args: schedule)
    monkeypatch.setattr(module, 'create_dispatcher', lambda *args: SimpleNamespace(feed_update=AsyncMock()))
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    async def scenario():
        with pytest.raises(fatal or asyncio.CancelledError):
            await module.run_bot(settings)
    asyncio.run(scenario())
    assert [c.args[0] for c in sleep.await_args_list] == [5, 10, 20, 40, 60, 60, 5]
    assert [c.kwargs['offset'] for c in bot.get_updates.await_args_list] == [None] + [72]*7 + [73]*3
    assert 'secret-url-token-and-payload' not in caplog.text
    schedule.stop.assert_awaited_once(); bot.session.close.assert_awaited_once()


def test_fingerprint_canonicalizes_json_and_includes_message_identity():
    assert Database.event_fingerprint(1, False, '{"chat":{"id":-10},"message_id":2}') == Database.event_fingerprint(1, False, '{ "message_id":2, "chat":{"id":-10} }')
    assert Database.event_fingerprint(1, False, '{"chat":{"id":-10},"message_id":2}') != Database.event_fingerprint(1, False, '{"chat":{"id":-20},"message_id":2}')
