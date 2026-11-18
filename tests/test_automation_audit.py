"""Automation audit regressions; fake Telegram only and temporary SQLite."""
import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_scheduler import service
from test_command_attempts import command
from app.config import Settings


def test_queued_scheduler_callback_does_not_start_after_stop(tmp_path):
    schedule = service(tmp_path)

    async def scenario():
        await schedule.stop()
        await schedule.scheduled()
        assert not schedule.tasks
        assert not schedule.history()
        schedule.generator.export.assert_not_called()
        schedule.bot.send_document.assert_not_awaited()

    asyncio.run(scenario())


def test_detached_scheduler_failure_is_retrieved_and_sanitized(tmp_path, caplog):
    schedule = service(tmp_path)

    async def scenario():
        async def failing():
            raise OSError('secret-content-and-token')
        unhandled = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: unhandled.append(context))
        task = schedule.spawn(failing())
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert not schedule.tasks
        assert not unhandled
        await schedule.stop()

    asyncio.run(scenario())
    assert 'Background schedule task failed (OSError)' in caplog.text
    assert 'secret-content-and-token' not in caplog.text


@pytest.mark.parametrize('notify_fails', [False, True])
def test_command_reply_failure_still_wakes_dirty_work_and_acknowledges(tmp_path, monkeypatch, notify_fails):
    module = importlib.import_module('app.bot.bot')
    path = tmp_path / 'schedule.json'
    path.write_text('{}')
    settings = Settings('unused', frozenset({42}), -1,
                        database_path=tmp_path/'db.sqlite', schedule_path=path)
    stop = RuntimeError('test polling boundary')
    bot = SimpleNamespace(get_webhook_info=AsyncMock(return_value=SimpleNamespace(url='')),
        get_me=AsyncMock(return_value=SimpleNamespace(username='offline')),
        get_updates=AsyncMock(side_effect=[[command()], stop]),
        session=SimpleNamespace(close=AsyncMock()))
    schedule = SimpleNamespace(start=Mock(), stop=AsyncMock(),
        notify_changes=AsyncMock(side_effect=OSError('private') if notify_fails else None))
    dispatcher = SimpleNamespace(feed_update=AsyncMock(side_effect=TimeoutError('reply uncertain')))
    monkeypatch.setattr(module, 'Bot', lambda token: bot)
    monkeypatch.setattr(module, 'ScheduleService', lambda *args: schedule)
    monkeypatch.setattr(module, 'create_dispatcher', lambda *args: dispatcher)
    with pytest.raises(RuntimeError, match='test polling boundary'):
        asyncio.run(module.run_bot(settings))
    schedule.notify_changes.assert_awaited_once()
    assert [c.kwargs['offset'] for c in bot.get_updates.await_args_list] == [None, command().update_id + 1]
    schedule.stop.assert_awaited_once()
    bot.session.close.assert_awaited_once()


def test_polling_network_backoff_retry_after_and_reset_keep_offset(tmp_path, monkeypatch):
    """Only safe reads retry; the cursor never advances on transport failure."""
    from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
    from aiogram.methods import GetUpdates
    module = importlib.import_module('app.bot.bot')
    path = tmp_path/'schedule.json'; path.write_text('{}')
    settings = Settings('unused', frozenset({42}), -1,
                        database_path=tmp_path/'db.sqlite', schedule_path=path)
    method = GetUpdates()
    network = TelegramNetworkError(method=method, message='private-url')
    rate_limit = TelegramRetryAfter(method=method, message='private-url', retry_after=7)
    stop = RuntimeError('test polling boundary')
    bot = SimpleNamespace(get_webhook_info=AsyncMock(return_value=SimpleNamespace(url='')),
        get_me=AsyncMock(return_value=SimpleNamespace(username='offline')),
        get_updates=AsyncMock(side_effect=[network]*6 + [rate_limit, [], network, stop]),
        session=SimpleNamespace(close=AsyncMock()))
    schedule = SimpleNamespace(start=Mock(), stop=AsyncMock(), notify_changes=AsyncMock())
    sleeps = AsyncMock()
    monkeypatch.setattr(module, 'Bot', lambda token: bot)
    monkeypatch.setattr(module, 'ScheduleService', lambda *args: schedule)
    monkeypatch.setattr(module.asyncio, 'sleep', sleeps)
    with pytest.raises(RuntimeError, match='test polling boundary'):
        asyncio.run(module.run_bot(settings))
    assert [c.args[0] for c in sleeps.await_args_list] == [5, 10, 20, 40, 60, 60, 7, 5]
    assert all(c.kwargs['offset'] is None for c in bot.get_updates.await_args_list)
    schedule.stop.assert_awaited_once()
    bot.session.close.assert_awaited_once()
