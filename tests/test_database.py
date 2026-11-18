from datetime import datetime, timezone
import sqlite3
import pytest
from aiogram.types import Update, Message, Chat, User
from app.database.database import Database


def update(uid, chat=-10, mid=1, text='Xin chào', edit=False):
    msg = Message(message_id=mid, date=datetime(2026, 11, 18, tzinfo=timezone.utc),
                  chat=Chat(id=chat, type='supergroup'),
                  from_user=User(id=42, is_bot=False, first_name='An'), text=text)
    return Update(update_id=uid, **{'edited_message' if edit else 'message': msg})


def test_three_groups_restart_and_dedup(tmp_path):
    path = tmp_path / 'collector.db'
    db = Database(path)
    for chat in (-10, -20, -30):
        db.register(chat, str(chat))
    for i in range(1000):
        assert db.collect(update(i, chat=(-10, -20, -30)[i % 3], mid=i+1))
    db = Database(path)
    for i in range(1000):
        assert not db.collect(update(i, chat=(-10, -20, -30)[i % 3], mid=i+1))
    assert db.stats() == {'messages': 1000, 'revisions': 1000, 'groups': 3}


def test_authorization_and_edit(tmp_path):
    db = Database(tmp_path / 'db')
    assert not db.collect(update(1))
    db.register(-10, 'Test')
    assert db.collect(update(1))
    assert db.collect(update(2, text='Đã sửa', edit=True))
    assert db.collect(update(3))  # A second update for the same message cannot duplicate it.
    with db.connect() as conn:
        assert conn.execute('SELECT content FROM raw_messages').fetchone()[0] == 'Đã sửa'
    assert db.stats()['messages'] == 1
    assert db.stats()['revisions'] == 3
    db.disable(-10)
    db.register(-10, 'Bootstrap', reactivate=False)
    assert not db.collect(update(4))


def test_private_not_collected(tmp_path):
    db = Database(tmp_path / 'db')
    private = Message(message_id=1, date=datetime(2026, 11, 18, tzinfo=timezone.utc), chat=Chat(id=42, type='private'), text='secret')
    assert not db.collect(Update(update_id=1, message=private))
    assert db.stats()['messages'] == 0


def test_failed_transaction_can_retry(tmp_path):
    db = Database(tmp_path / 'db')
    db.register(-10, 'Test')
    with db.connect() as conn:
        conn.execute("CREATE TRIGGER fail_revision BEFORE INSERT ON message_revisions BEGIN SELECT RAISE(ABORT, 'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.collect(update(1))
    assert db.stats()['messages'] == 0
    with db.connect() as conn:
        conn.execute('DROP TRIGGER fail_revision')
    assert db.collect(update(1))
