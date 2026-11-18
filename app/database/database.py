import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 7:
                raise RuntimeError("Database schema is newer than this application; upgrade the application.")
            # DDL and data migration must commit together; executescript would
            # implicitly commit before running, so execute statements individually.
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('BEGIN IMMEDIATE')
            schema = """
            CREATE TABLE IF NOT EXISTS telegram_groups (
                chat_id INTEGER PRIMARY KEY, display_name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS raw_messages (
                id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES telegram_groups(chat_id),
                message_id INTEGER NOT NULL, user_id INTEGER, username TEXT, employee_name TEXT,
                sender_chat_id INTEGER, submitted_at TEXT NOT NULL, content TEXT,
                message_json TEXT NOT NULL, processing_status TEXT NOT NULL DEFAULT 'PENDING',
                processing_error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS message_revisions (
                update_id INTEGER PRIMARY KEY,
                raw_message_id INTEGER NOT NULL REFERENCES raw_messages(id),
                is_edit INTEGER NOT NULL, message_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_messages_group ON raw_messages(chat_id);
            CREATE INDEX IF NOT EXISTS ix_revisions_message ON message_revisions(raw_message_id,update_id);
            CREATE TABLE IF NOT EXISTS reports (
                raw_message_id INTEGER PRIMARY KEY REFERENCES raw_messages(id),
                source_revision_id INTEGER REFERENCES message_revisions(update_id),
                report_code TEXT, code_key TEXT, report_date TEXT,
                fields_json TEXT NOT NULL, base_status TEXT NOT NULL
                    CHECK(base_status IN ('VALID','INVALID','NEED_REVIEW')),
                status TEXT NOT NULL CHECK(status IN ('VALID','INVALID','DUPLICATE','NEED_REVIEW')),
                issues_json TEXT NOT NULL, duplicate_of INTEGER REFERENCES raw_messages(id),
                rules_hash TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_reports_code ON reports(code_key);
            CREATE INDEX IF NOT EXISTS ix_reports_date ON reports(report_date);
            CREATE TABLE IF NOT EXISTS report_history (
                id INTEGER PRIMARY KEY, raw_message_id INTEGER NOT NULL REFERENCES raw_messages(id),
                source_revision_id INTEGER REFERENCES message_revisions(update_id),
                status TEXT NOT NULL, fields_json TEXT NOT NULL, issues_json TEXT NOT NULL,
                duplicate_of INTEGER REFERENCES raw_messages(id), rules_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS export_history (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, report_date TEXT NOT NULL,
                chat_id INTEGER, requested_by INTEGER, file_path TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('STARTED','SUCCESS','FAILED')),
                error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_export_history_date ON export_history(report_date);
            CREATE TABLE IF NOT EXISTS scheduled_runs (
                id INTEGER PRIMARY KEY, slot_key TEXT NOT NULL UNIQUE,
                report_date TEXT NOT NULL, chat_id INTEGER, trigger_type TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS realtime_dirty (
                report_date TEXT NOT NULL, chat_id INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, completed_revision INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(report_date,chat_id)
            );
            CREATE TABLE IF NOT EXISTS report_export_state (
                raw_message_id INTEGER PRIMARY KEY, report_date TEXT NOT NULL,
                chat_id INTEGER NOT NULL, projection TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS realtime_attempts (
                id INTEGER PRIMARY KEY, report_date TEXT NOT NULL, revisions_json TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            );
            """
            if 'id' in {r['name'] for r in db.execute('PRAGMA table_info(message_revisions)')}:
                schema = schema.replace('REFERENCES message_revisions(update_id)', 'REFERENCES message_revisions(id)')
            for statement in schema.split(';'):
                if statement.strip():
                    db.execute(statement)
            if version < 6 and 'id' not in {r['name'] for r in db.execute('PRAGMA table_info(message_revisions)')}:
                self._migrate_revisions(db)
            if 'current_revision_id' not in {r['name'] for r in db.execute('PRAGMA table_info(raw_messages)')}:
                db.execute('ALTER TABLE raw_messages ADD COLUMN current_revision_id INTEGER REFERENCES message_revisions(id)')
                db.execute('''UPDATE raw_messages SET current_revision_id=(
                    SELECT id FROM message_revisions r WHERE r.raw_message_id=raw_messages.id
                    AND r.message_json=raw_messages.message_json ORDER BY r.id DESC LIMIT 1)''')
            db.execute('''CREATE TABLE IF NOT EXISTS admin_command_attempts (
                event_key TEXT PRIMARY KEY, command TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('CLAIMED','SUCCESS','FAILED','UNCERTAIN')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT)''')
            # A process may have died after a claim or after an external effect.
            db.execute("UPDATE admin_command_attempts SET status='UNCERTAIN',completed_at=CURRENT_TIMESTAMP WHERE status='CLAIMED'")
            db.execute('PRAGMA user_version=7')
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise sqlite3.IntegrityError('Foreign key check failed during migration')
            if version < 5:
                self.record_export_changes(db, enqueue=False)

    @staticmethod
    def event_fingerprint(update_id, is_edit, payload):
        """Canonical payload identity, independent of JSON key order/whitespace."""
        canonical = json.dumps([update_id, bool(is_edit), json.loads(payload)],
                               sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

    @classmethod
    def _migrate_revisions(cls, db):
        # Keep old update IDs as internal IDs so every existing audit reference
        # is preserved exactly. Future inserts allocate independent internal IDs.
        db.execute("""CREATE TABLE revisions_v6 (
            id INTEGER PRIMARY KEY, update_id INTEGER NOT NULL,
            event_fingerprint TEXT NOT NULL UNIQUE,
            raw_message_id INTEGER NOT NULL REFERENCES raw_messages(id),
            is_edit INTEGER NOT NULL, message_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        for row in db.execute('SELECT * FROM message_revisions').fetchall():
            db.execute('INSERT INTO revisions_v6 VALUES (?,?,?,?,?,?,?)',
                       (row['update_id'], row['update_id'],
                        cls.event_fingerprint(row['update_id'], row['is_edit'], row['message_json']),
                        row['raw_message_id'], row['is_edit'], row['message_json'], row['created_at']))
        for table in ('reports', 'report_history'):
            sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            sql = sql.replace('CREATE TABLE ' + table, 'CREATE TABLE ' + table + '_v6', 1)
            sql = sql.replace('message_revisions(update_id)', 'message_revisions(id)')
            db.execute(sql)
            db.execute(f'INSERT INTO {table}_v6 SELECT * FROM {table}')
            db.execute(f'DROP TABLE {table}')
        db.execute('DROP TABLE message_revisions')
        db.execute('ALTER TABLE revisions_v6 RENAME TO message_revisions')
        for table in ('reports', 'report_history'):
            db.execute(f'ALTER TABLE {table}_v6 RENAME TO {table}')
        db.execute('CREATE INDEX ix_revisions_message ON message_revisions(raw_message_id,id)')
        db.execute('CREATE INDEX ix_revisions_update ON message_revisions(update_id)')
        db.execute('CREATE INDEX ix_reports_code ON reports(code_key)')
        db.execute('CREATE INDEX ix_reports_date ON reports(report_date)')

    @staticmethod
    def record_export_changes(db, enqueue=True, raw_ids=None):
        """Persist the committed VALID projection, not transient raw PENDING state.

        Called inside the report write transaction, including duplicate reconciliation.
        The stored projection preserves old identity/date even after raw edits commit.
        """
        # A scoped update avoids reading/rewriting every exported report per message.
        # Callers include both the edited report and every duplicate whose state changed.
        if raw_ids is not None:
            raw_ids = tuple(sorted(set(raw_ids)))
            if not raw_ids:
                return set()
            # Extremely large duplicate families still use the bounded-query
            # full sync rather than exceed SQLite's host-parameter limit.
            if len(raw_ids) > 500:
                raw_ids = None
        where = '' if raw_ids is None else ' WHERE raw_message_id IN (' + ','.join('?' for _ in raw_ids) + ')'
        params = () if raw_ids is None else raw_ids
        previous = {r['raw_message_id']: (r['report_date'], r['chat_id'], r['projection'])
                    for r in db.execute('SELECT * FROM report_export_state' + where, params)}
        current = {}
        query = """SELECT r.raw_message_id,r.report_date,r.fields_json,
                m.chat_id,m.user_id,m.message_id FROM reports r
                JOIN raw_messages m ON m.id=r.raw_message_id WHERE r.status='VALID'"""
        if raw_ids is not None:
            query += ' AND r.raw_message_id IN (' + ','.join('?' for _ in raw_ids) + ')'
        for r in db.execute(query, params):
            projection = json.dumps([json.loads(r['fields_json']), r['user_id'], r['message_id']],
                                    sort_keys=True, ensure_ascii=False)
            current[r['raw_message_id']] = (r['report_date'], r['chat_id'], projection)
        affected = set()
        for key in previous.keys() | current.keys():
            if previous.get(key) != current.get(key):
                for state in (previous.get(key), current.get(key)):
                    if state:
                        affected.add(state[:2])
        if enqueue:
            for day, chat_id in affected:
                db.execute("""INSERT INTO realtime_dirty(report_date,chat_id) VALUES (?,?)
                    ON CONFLICT(report_date,chat_id) DO UPDATE SET revision=revision+1,
                    updated_at=CURRENT_TIMESTAMP""", (day, chat_id))
        db.execute('DELETE FROM report_export_state' + where, params)
        db.executemany('INSERT INTO report_export_state VALUES (?,?,?,?)',
                       [(key, *value) for key, value in current.items()])
        return affected

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim_command(self, event_key, command):
        """Commit an attempt before any effect; all existing statuses suppress replay."""
        with self.connect() as db:
            return bool(db.execute("INSERT OR IGNORE INTO admin_command_attempts(event_key,command,status) VALUES (?,?,'CLAIMED')",
                                   (event_key, command)).rowcount)

    def finish_command(self, event_key, status):
        if status not in ('SUCCESS', 'FAILED', 'UNCERTAIN'):
            raise ValueError('Invalid command status')
        with self.connect() as db:
            db.execute("UPDATE admin_command_attempts SET status=?,completed_at=CURRENT_TIMESTAMP WHERE event_key=?", (status, event_key))

    def register(self, chat_id, name, reactivate=True):
        with self.connect() as db:
            if reactivate:
                db.execute('''INSERT INTO telegram_groups(chat_id,display_name) VALUES (?,?)
                    ON CONFLICT(chat_id) DO UPDATE SET display_name=excluded.display_name,
                    active=1,updated_at=CURRENT_TIMESTAMP''', (chat_id, name))
            else:
                db.execute('INSERT OR IGNORE INTO telegram_groups(chat_id,display_name) VALUES (?,?)', (chat_id, name))

    def disable(self, chat_id):
        with self.connect() as db:
            return db.execute('UPDATE telegram_groups SET active=0,updated_at=CURRENT_TIMESTAMP WHERE chat_id=?', (chat_id,)).rowcount

    def groups(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM telegram_groups ORDER BY chat_id')]

    def stats(self):
        with self.connect() as db:
            return dict(messages=db.execute('SELECT COUNT(*) FROM raw_messages').fetchone()[0],
                        revisions=db.execute('SELECT COUNT(*) FROM message_revisions').fetchone()[0],
                        groups=db.execute('SELECT COUNT(*) FROM telegram_groups WHERE active=1').fetchone()[0])

    def collect(self, update):
        message = update.message or update.edited_message
        if not message or message.chat.type not in ('group', 'supergroup'):
            return False
        with self.connect() as db:
            # Registration and authorization are checked in the same write transaction.
            db.execute('BEGIN IMMEDIATE')
            group = db.execute('SELECT active FROM telegram_groups WHERE chat_id=?', (message.chat.id,)).fetchone()
            if not group or not group['active']:
                return False
            payload = message.model_dump_json(exclude_none=True)
            fingerprint = self.event_fingerprint(update.update_id, update.edited_message is not None, payload)
            if db.execute('SELECT 1 FROM message_revisions WHERE event_fingerprint=?', (fingerprint,)).fetchone():
                return False
            sender = message.from_user
            existing = db.execute('SELECT id,message_json,current_revision_id FROM raw_messages WHERE chat_id=? AND message_id=?',
                                  (message.chat.id, message.message_id)).fetchone()
            values = (sender.id if sender else None, sender.username if sender else None,
                      sender.full_name if sender else None,
                      message.sender_chat.id if message.sender_chat else None,
                      message.date.isoformat(), message.text or message.caption, payload)
            accept_revision = not existing
            if existing:
                raw_id = existing['id']
                # Keep revisions for replay and audit; only an edit changes an existing message.
                accepted = db.execute('SELECT update_id FROM message_revisions WHERE id=?', (existing['current_revision_id'],)).fetchone()
                latest = accepted['update_id'] if accepted else None
                previous = json.loads(existing['message_json'])
                old_edit = previous.get('edit_date', previous.get('date'))
                new_edit = json.loads(payload).get('edit_date')
                # Telegram timestamps take precedence (update IDs may reset after inactivity).
                newer = ((new_edit > old_edit or (new_edit == old_edit and (latest is None or update.update_id > latest))) if new_edit is not None and old_edit is not None
                         else latest is None or update.update_id > latest)
                if update.edited_message and newer:
                    accept_revision = True
                    db.execute('''UPDATE raw_messages SET user_id=?,username=?,employee_name=?,sender_chat_id=?,
                        submitted_at=?,content=?,message_json=?,processing_status='PENDING',processing_error=NULL,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?''', (*values, raw_id))
            else:
                raw_id = db.execute('''INSERT INTO raw_messages
                    (chat_id,message_id,user_id,username,employee_name,sender_chat_id,submitted_at,content,message_json)
                    VALUES (?,?,?,?,?,?,?,?,?)''', (message.chat.id, message.message_id, *values)).lastrowid
            revision_id = db.execute('INSERT INTO message_revisions(update_id,raw_message_id,is_edit,message_json,event_fingerprint) VALUES (?,?,?,?,?)',
                       (update.update_id, raw_id, int(update.edited_message is not None), payload, fingerprint)).lastrowid
            if accept_revision:
                db.execute('UPDATE raw_messages SET current_revision_id=? WHERE id=?', (revision_id, raw_id))
            return True

    def raw_id_for_update(self, update):
        with self.connect() as db:
            if isinstance(update, int):
                # Legacy callers may use an ID only if its meaning is unambiguous.
                rows = db.execute('SELECT raw_message_id FROM message_revisions WHERE update_id=?', (update,)).fetchall()
                return rows[0][0] if len(rows) == 1 else None
            message = update.message or update.edited_message
            if message is None:
                return None
            fingerprint = self.event_fingerprint(update.update_id, update.edited_message is not None,
                                                  message.model_dump_json(exclude_none=True))
            row = db.execute('SELECT raw_message_id FROM message_revisions WHERE event_fingerprint=?', (fingerprint,)).fetchone()
            return row[0] if row else None

    def report_today(self, day):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT status,COUNT(*) AS count FROM reports WHERE report_date=? GROUP BY status ORDER BY status", (day,))]

    def errors(self, limit=50):
        with self.connect() as db:
            return [dict(r) for r in db.execute('''SELECT m.id,m.chat_id,m.message_id,m.processing_status,
                m.processing_error,r.status,r.issues_json,r.duplicate_of FROM raw_messages m
                LEFT JOIN reports r ON r.raw_message_id=m.id
                WHERE m.processing_status='PENDING' OR r.status IN ('INVALID','DUPLICATE','NEED_REVIEW')
                ORDER BY m.id DESC LIMIT ?''', (limit,))]
