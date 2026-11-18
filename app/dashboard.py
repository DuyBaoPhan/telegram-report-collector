"""Authenticated loopback-only, read-only collector monitoring server.

Run separately with ``python -m app.dashboard``. No collector initialization/migrations.
"""
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from app.heartbeat import heartbeat_path

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).with_name('dashboard_static')


@dataclass(frozen=True)
class DashboardSettings:
    database_path: Path
    username: str
    password: str
    port: int = 8000

    def __post_init__(self):
        if not self.username or ':' in self.username or len(self.password) < 12:
            raise ValueError('Set DASHBOARD_USERNAME and DASHBOARD_PASSWORD (at least 12 characters).')
        if not 1024 <= self.port <= 65535:
            raise ValueError('DASHBOARD_PORT must be between 1024 and 65535.')

    @classmethod
    def from_env(cls):
        # Deliberately do not load .env / BOT_TOKEN. Dashboard needs only its own credentials.
        path = Path(os.getenv('DATABASE_PATH', 'data/collector.db'))
        return cls(path if path.is_absolute() else ROOT / path,
                   os.getenv('DASHBOARD_USERNAME', 'admin'), os.getenv('DASHBOARD_PASSWORD', ''),
                   int(os.getenv('DASHBOARD_PORT', '8000')))


@contextmanager
def read_database(path):
    # Never create an absent database and never instantiate the collector Database class.
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')  # coherent snapshot across all aggregates
        yield connection
    finally:
        connection.close()


def runtime_status(path):
    unknown = {'state': 'unknown', 'last_poll_at': None, 'updated_at': None, 'age_seconds': None}
    try:
        source = heartbeat_path(path)
        if source.stat().st_size > 4096:
            return unknown
        data = json.loads(source.read_text(encoding='utf-8'))
        updated = datetime.fromisoformat(data['updated_at'])
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        state = data.get('state')
        if state not in {'starting', 'polling', 'retrying', 'stopped'}:
            return unknown
        if age < -5:
            return unknown
        return {'state': 'stale' if age > 90 and state != 'stopped' else state,
                'last_poll_at': data.get('last_poll_at'), 'updated_at': data['updated_at'],
                'age_seconds': max(0, int(age))}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return unknown


def change_token(path):
    """Cheap invalidation hint, not an event log or a transaction snapshot.

    Include WAL metadata: the main DB alone may not change until checkpoint.
    No DB connection, migrations, or files are created by this check.
    """
    path = Path(path)
    metadata = []
    for source in (path, Path(str(path) + '-wal'), heartbeat_path(path)):
        try:
            info = source.stat()
            metadata.append((info.st_dev, info.st_ino, info.st_size,
                             info.st_mtime_ns, info.st_ctime_ns))
        except FileNotFoundError:
            if source == path:
                raise
            metadata.append(None)
    # Staleness and local report-day rollover can change without file writes.
    metadata.extend([runtime_status(path)['state'],
                     datetime.now(timezone(timedelta(hours=7))).date().isoformat()])
    return hashlib.sha256(json.dumps(metadata).encode()).hexdigest()


def rows(db, sql, parameters=()):
    return [dict(row) for row in db.execute(sql, parameters)]


def create_app(settings):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def security(request, call_next):
        allowed = False
        try:
            scheme, encoded = request.headers.get('authorization', '').split(' ', 1)
            user, password = base64.b64decode(encoded, validate=True).decode('utf-8').split(':', 1)
            allowed = scheme.lower() == 'basic' and secrets.compare_digest(user.encode(), settings.username.encode())
            allowed = secrets.compare_digest(password.encode(), settings.password.encode()) and allowed
        except (ValueError, UnicodeError):
            pass
        if not allowed:
            response = Response(status_code=401, headers={'WWW-Authenticate': 'Basic realm="Collector monitor", charset="UTF-8"'})
        elif request.method not in {'GET', 'HEAD'}:
            response = Response(status_code=405)
        else:
            response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})
        return response

    @app.get('/')
    def index():
        return FileResponse(STATIC / 'index.html')

    @app.get('/assets/{name}')
    def asset(name: str):
        if name not in {'dashboard.css', 'dashboard.js'}:
            return Response(status_code=404)
        return FileResponse(STATIC / name)

    @app.get('/api/changes')
    def changes():
        try:
            return {'token': change_token(settings.database_path)}
        except OSError:
            return JSONResponse({'error': 'database_unavailable'}, status_code=503)

    @app.get('/api/overview')
    def overview():
        heartbeat = runtime_status(settings.database_path)
        try:
            with read_database(settings.database_path) as db:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version != 7:
                    return JSONResponse({'error': 'unsupported_schema', 'runtime': heartbeat}, status_code=503)
                counts = dict(db.execute('''SELECT count(*) messages,
                    COALESCE(sum(processing_status='PENDING'),0) pending,
                    COALESCE(sum(processing_error IS NOT NULL),0) processing_errors FROM raw_messages''').fetchone())
                counts['groups'] = db.execute('SELECT count(*) FROM telegram_groups WHERE active=1').fetchone()[0]
                counts['today'] = db.execute("SELECT count(*) FROM raw_messages WHERE date(submitted_at,'+7 hours')=date('now','+7 hours')").fetchone()[0]
                statuses = rows(db, 'SELECT status,count(*) count FROM reports GROUP BY status')
                start = datetime.now(timezone(timedelta(hours=7))).date() - timedelta(days=6)
                found = {r['day']: r['count'] for r in rows(db, "SELECT date(submitted_at,'+7 hours') day,count(*) count FROM raw_messages WHERE submitted_at>=? GROUP BY day", ((start - timedelta(days=1)).isoformat(),))}
                daily = [{'day': (start+timedelta(days=i)).isoformat(), 'count': found.get((start+timedelta(days=i)).isoformat(),0)} for i in range(7)]
                history = rows(db, '''SELECT id,report_date,chat_id,status,created_at,completed_at FROM export_history ORDER BY id DESC LIMIT 8''')
                scheduled = rows(db, '''SELECT id,report_date,chat_id,trigger_type,status,created_at,completed_at FROM scheduled_runs ORDER BY id DESC LIMIT 8''')
                realtime = rows(db, 'SELECT status,count(*) count FROM realtime_attempts GROUP BY status')
                counts['dirty'] = db.execute('SELECT count(*) FROM realtime_dirty WHERE revision>completed_revision').fetchone()[0]
                return {'database': 'ready', 'schema': version, 'runtime': heartbeat, 'counts': counts,
                    'statuses': statuses, 'daily': daily, 'exports': history, 'scheduled': scheduled,
                    'realtime': realtime, 'generated_at': datetime.now(timezone.utc).isoformat()}
        except sqlite3.Error:
            return JSONResponse({'error': 'database_unavailable', 'runtime': heartbeat}, status_code=503)

    @app.get('/api/groups')
    def groups(q: str = Query('', max_length=100), active: str = Query('all', pattern='^(all|active|inactive)$'),
               page: int = Query(1, ge=1, le=100000), limit: int = Query(10, ge=1, le=50)):
        where = 'WHERE (g.display_name LIKE ? ESCAPE \'\\\' OR CAST(g.chat_id AS TEXT) LIKE ? ESCAPE \'\\\')'
        term = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        parameters = [term, term]
        if active != 'all':
            where += ' AND g.active=?'
            parameters.append(int(active == 'active'))
        try:
            with read_database(settings.database_path) as db:
                if db.execute('PRAGMA user_version').fetchone()[0] != 7:
                    return JSONResponse({'error': 'unsupported_schema'}, status_code=503)
                total = db.execute('SELECT count(*) FROM telegram_groups g '+where, parameters).fetchone()[0]
                items = rows(db, '''SELECT g.chat_id,g.display_name,g.active,
                    (SELECT count(*) FROM raw_messages m WHERE m.chat_id=g.chat_id) messages,
                    (SELECT max(submitted_at) FROM raw_messages m WHERE m.chat_id=g.chat_id) last_message,
                    (SELECT count(*) FROM reports r JOIN raw_messages m ON m.id=r.raw_message_id WHERE m.chat_id=g.chat_id AND r.status IN ('INVALID','NEED_REVIEW')) attention
                    FROM telegram_groups g '''+where+' ORDER BY g.active DESC,g.display_name,g.chat_id LIMIT ? OFFSET ?', parameters+[limit,(page-1)*limit])
                return {'items': items, 'total': total, 'page': page, 'limit': limit}
        except sqlite3.Error:
            return JSONResponse({'error': 'database_unavailable'}, status_code=503)

    @app.get('/api/messages')
    def messages(page: int = Query(1, ge=1, le=100000), limit: int = Query(10, ge=1, le=50),
                 status: str = Query('all', pattern='^(all|PENDING|VALID|INVALID|DUPLICATE|NEED_REVIEW)$')):
        where = '' if status == 'all' else ('WHERE m.processing_status=?' if status == 'PENDING' else 'WHERE r.status=?')
        values = [] if status == 'all' else [status]
        try:
            with read_database(settings.database_path) as db:
                if db.execute('PRAGMA user_version').fetchone()[0] != 7:
                    return JSONResponse({'error': 'unsupported_schema'}, status_code=503)
                join = ' FROM raw_messages m LEFT JOIN reports r ON r.raw_message_id=m.id LEFT JOIN telegram_groups g ON g.chat_id=m.chat_id '
                total = db.execute('SELECT count(*)'+join+where, values).fetchone()[0]
                items = rows(db, '''SELECT m.id,m.chat_id,m.message_id,g.display_name,m.submitted_at,
                    m.processing_status,r.status report_status,
                    CASE WHEN m.processing_error IS NOT NULL THEN 1 ELSE 0 END has_error'''+join+where+
                    ' ORDER BY m.id DESC LIMIT ? OFFSET ?', values+[limit,(page-1)*limit])
                return {'items':items,'total':total,'page':page,'limit':limit}
        except sqlite3.Error:
            return JSONResponse({'error':'database_unavailable'}, status_code=503)
    return app


def main():
    import uvicorn
    try:
        settings = DashboardSettings.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    # No CLI host override: never expose Basic credentials beyond loopback.
    uvicorn.run(create_app(settings), host='127.0.0.1', port=settings.port, access_log=False)


if __name__ == '__main__':
    main()

