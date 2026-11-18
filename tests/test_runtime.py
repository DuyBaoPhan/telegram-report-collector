import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, Update

from app.database.database import Database
from app.parser import ReportParser, ReportProcessor
from app.runtime import SafeLogFilter, instance_lock


def test_future_schema_refused_without_mutation(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="newer"):
        Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 99
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0


def test_out_of_order_edit_and_source_revision(tmp_path):
    db = Database(tmp_path / "db")
    db.register(-10, "group")
    now = datetime(2026, 11, 18, tzinfo=timezone.utc)
    def update(uid, text, seconds, edited=True):
        msg = Message(message_id=1, chat=Chat(id=-10, type="supergroup"),
                      date=now, text=text, edit_date=int((now + timedelta(seconds=seconds)).timestamp()) if edited else None)
        return Update(update_id=uid, **{"edited_message" if edited else "message": msg})
    db.collect(update(1, "original", 0, False))
    db.collect(update(3, "new", 20))
    db.collect(update(2, "stale", 10))
    db.collect(update(4, "stale original", 0, False))
    with db.connect() as conn:
        assert conn.execute("SELECT content FROM raw_messages").fetchone()[0] == "new"
    # A timestamp-newer edit is accepted even if the update ID has reset.
    db.collect(update(0, "newest", 30))
    from pathlib import Path
    proc = ReportProcessor(db, ReportParser(Path(__file__).resolve().parents[1] / "config/report_rules.json"))
    assert proc.process(1)
    with db.connect() as conn:
        assert conn.execute("SELECT content FROM raw_messages").fetchone()[0] == "newest"
        assert conn.execute("SELECT r.update_id FROM report_history h JOIN message_revisions r ON r.id=h.source_revision_id ORDER BY h.id DESC").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM message_revisions").fetchone()[0] == 5


def test_local_lock_released_and_reentrant_refused(tmp_path):
    path = tmp_path / "db"
    with instance_lock(path):
        with pytest.raises(RuntimeError, match="Another local"):
            with instance_lock(path):
                pytest.fail("second instance acquired lock")
    with instance_lock(path):
        pass


def test_safe_log_filter_redacts_and_discards_dependency_payloads():
    token = "123456:abcdefghijklmnop_secret"
    record = logging.LogRecord("app.test", logging.ERROR, "", 1,
                               "failed %s", (token,), (ValueError, ValueError(token), None))
    assert SafeLogFilter(token).filter(record)
    assert token not in record.getMessage()
    assert record.exc_info is None
    record.name = "aiogram.event"
    assert not SafeLogFilter(token).filter(record)


def test_main_sanitizes_startup_exception(tmp_path, monkeypatch, capsys):
    import app.main as module
    settings = SimpleNamespace(bot_token="secret-token", log_level="INFO", database_path=tmp_path / "db")
    monkeypatch.setattr(module.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(module, "configure_logging", lambda *args: None)
    async def fail(settings):
        raise ConnectionError("https://api.telegram.org/botsecret-token/getMe")
    monkeypatch.setattr(module, "run_bot", fail)
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == 1
    output = capsys.readouterr()
    assert "secret-token" not in output.out + output.err
    assert "ConnectionError" in output.out
