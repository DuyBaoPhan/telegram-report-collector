"""Deterministic label-based reports. No inferred employee, date or report values."""
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def normalize(value):
    return ' '.join(unicodedata.normalize('NFC', value).casefold().split())


@dataclass(frozen=True)
class ParseResult:
    fields: dict
    status: str | None
    issues: list[str]


class ReportParser:
    def __init__(self, config_path):
        self.config = json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
        c = self.config
        fields = c.get('labels', {})
        if not isinstance(fields, dict) or not {'report_code', 'report_date'}.issubset(fields):
            raise ValueError('labels must include report_code and report_date')
        self.aliases = {}
        for field, aliases in fields.items():
            if not isinstance(aliases, list) or not aliases:
                raise ValueError('Each field needs a nonempty label list')
            for label in aliases:
                if not isinstance(label, str) or not normalize(label):
                    raise ValueError('Labels must be nonempty strings')
                key = normalize(label)
                if key in self.aliases:
                    raise ValueError('Duplicate label alias')
                self.aliases[key] = field
        self.required = c.get('required_fields', ['report_code', 'report_date'])
        if (not isinstance(self.required, list) or any(not isinstance(f, str) for f in self.required)
                or not set(self.required).issubset(fields)
                or not {'report_code', 'report_date'}.issubset(self.required)):
            raise ValueError('required_fields must include report_code and report_date and contain only known fields')
        multiline = c.get('multiline_fields', ['work_content'] if 'work_content' in fields else [])
        if (not isinstance(multiline, list) or any(not isinstance(f, str) for f in multiline)
                or not set(multiline).issubset(fields)):
            raise ValueError('multiline_fields must contain only known fields')
        self.formats = c.get('date_formats', ['%d/%m/%Y', '%Y-%m-%d'])
        if not isinstance(self.formats, list) or not self.formats:
            raise ValueError('date_formats must contain explicit year, month and day')
        for fmt in self.formats:
            # Escaped percent literals (%%Y) are not year directives. Without
            # this check strptime silently invents the default year 1900.
            directives = re.findall(r'%(.)', fmt.replace('%%', '')) if isinstance(fmt, str) else []
            if not {'Y', 'm', 'd'}.issubset(directives):
                raise ValueError('date_formats must contain explicit year, month and day')
            try:
                sample = datetime(2000, 2, 29)
                datetime.strptime(sample.strftime(fmt), fmt)
            except (ValueError, TypeError) as exc:
                raise ValueError('Invalid date format') from exc
        self.timezone = ZoneInfo(c.get('timezone', 'Asia/Ho_Chi_Minh'))
        self.policies = c.get('policies', {})
        if not isinstance(self.policies, dict):
            raise ValueError('policies must be an object')
        for key in ('employee_ids', 'group_ids'):
            value = self.policies.get(key)
            if value is not None and (not isinstance(value, list) or any(type(i) is not int for i in value)):
                raise ValueError(f'{key} must be a list of integer IDs or null')
        hours = self.policies.get('submission_hours')
        if hours is not None and (not isinstance(hours, list) or len(hours) != 2 or any(type(i) is not int or not 0 <= i <= 23 for i in hours)):
            raise ValueError('submission_hours must be [start_hour, end_hour] or null')
        self.fingerprint = hashlib.sha256(json.dumps(c, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def parse(self, text, *, user_id=None, chat_id=None, submitted_at=None):
        values, issues = {}, []
        current = None
        header = False
        code_seen = False
        for line in (text or '').splitlines():
            line = line.strip()
            if not line:
                continue
            if normalize(line) in ('báo cáo', 'bao cao'):
                header = True
                current = None
                continue
            match = re.match(r'^([^:：]+)[:：]\s*(.*)$', line)
            field = self.aliases.get(normalize(match[1])) if match else None
            if field:
                code_seen |= field == 'report_code'
                value = match[2].strip()
                if field in values:
                    issues.append(f'repeated_field:{field}')
                    # Do not silently select one of conflicting values.
                else:
                    values[field] = value
                current = field
            elif current and current in self.config.get('multiline_fields', ['work_content']):
                values[current] += '\n' + line
            else:
                current = None
        if not (header or code_seen or len(values) >= 2):
            return ParseResult({}, None, [])  # ordinary chat, not an invalid report
        errors = [f'missing:{f}' for f in self.required if not values.get(f, '').strip()]
        if values.get('report_date'):
            parsed_dates = set()
            for fmt in self.formats:
                try:
                    parsed_dates.add(datetime.strptime(values['report_date'], fmt).date().isoformat())
                except ValueError:
                    pass
            if not parsed_dates:
                errors.append('invalid_date:report_date')
            elif len(parsed_dates) > 1:
                issues.append('ambiguous_date:report_date')
            else:
                values['report_date'] = parsed_dates.pop()
        for policy, actual in (('employee_ids', user_id), ('group_ids', chat_id)):
            allowed = self.policies.get(policy)
            if allowed is None:
                issues.append(f'policy_not_configured:{policy}')
            elif actual not in allowed:
                errors.append(f'not_allowed:{policy}')
        hours = self.policies.get('submission_hours')
        if hours is None:
            issues.append('policy_not_configured:submission_hours')
        else:
            try:
                when = datetime.fromisoformat(submitted_at)
                if when.tzinfo is None:
                    raise ValueError('Naive date')
                hour = when.astimezone(self.timezone).hour
                start, end = hours
                allowed = start <= hour <= end if start <= end else hour >= start or hour <= end
                if not allowed:
                    errors.append('outside_submission_hours')
            except (ValueError, TypeError):
                issues.append('unknown_submission_time')
        return ParseResult(values, 'INVALID' if errors else 'NEED_REVIEW' if issues else 'VALID', errors + issues)

class ReportProcessor:
    """Raw collection commits separately; any processing failure leaves it replayable."""
    def __init__(self, database, parser):
        self.database = database
        self.parser = parser

    def process(self, raw_id, force=False):
        try:
            with self.database.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                raw = db.execute('SELECT * FROM raw_messages WHERE id=?', (raw_id,)).fetchone()
                if raw is None or (not force and raw['processing_status'] != 'PENDING'):
                    return False
                old_report = db.execute('SELECT code_key FROM reports WHERE raw_message_id=?', (raw_id,)).fetchone()
                result = self.parser.parse(raw['content'], user_id=raw['user_id'],
                    chat_id=raw['chat_id'], submitted_at=raw['submitted_at'])
                revision = db.execute('SELECT id FROM message_revisions WHERE id=?', (raw['current_revision_id'],)).fetchone()
                revision_id = revision[0] if revision else None
                if result.status is None:
                    db.execute('DELETE FROM reports WHERE raw_message_id=?', (raw_id,))
                    self._history(db, raw_id, revision_id, 'IGNORED', {}, [], None)
                else:
                    code = result.fields.get('report_code', '').strip()
                    code_key = normalize(code) if code else None
                    db.execute('''INSERT INTO reports(raw_message_id,source_revision_id,report_code,code_key,
                        report_date,fields_json,base_status,status,issues_json,rules_hash)
                        VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(raw_message_id) DO UPDATE SET
                        source_revision_id=excluded.source_revision_id,report_code=excluded.report_code,
                        code_key=excluded.code_key,report_date=excluded.report_date,fields_json=excluded.fields_json,
                        base_status=excluded.base_status,status=excluded.status,issues_json=excluded.issues_json,
                        duplicate_of=NULL,rules_hash=excluded.rules_hash,updated_at=CURRENT_TIMESTAMP''',
                        (raw_id, revision_id, code or None, code_key, result.fields.get('report_date'),
                         json.dumps(result.fields, ensure_ascii=False), result.status, result.status,
                         json.dumps(result.issues, ensure_ascii=False), self.parser.fingerprint))
                    self._history(db, raw_id, revision_id, result.status, result.fields, result.issues, None)
                db.execute('''UPDATE raw_messages SET processing_status=?,processing_error=NULL,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?''', ('IGNORED' if result.status is None else 'PROCESSED', raw_id))
                code_keys = {old_report['code_key'] if old_report else None,
                             normalize(result.fields.get('report_code', '').strip()) or None}
                changed_ids = self._reconcile_duplicates(db, code_keys)
                self.database.record_export_changes(db, raw_ids=changed_ids | {raw_id})
                return True
        except Exception as exc:
            # Never store payload or exception text; parser/DB failures do not acknowledge processing.
            with self.database.connect() as db:
                db.execute("UPDATE raw_messages SET processing_status='PENDING',processing_error=? WHERE id=?",
                           (type(exc).__name__, raw_id))
            raise

    def _history(self, db, raw_id, revision, status, fields, issues, duplicate_of, rules_hash=None):
        db.execute('''INSERT INTO report_history(raw_message_id,source_revision_id,status,fields_json,
            issues_json,duplicate_of,rules_hash) VALUES (?,?,?,?,?,?,?)''',
            (raw_id, revision, status, json.dumps(fields, ensure_ascii=False),
             json.dumps(issues, ensure_ascii=False), duplicate_of,
             self.parser.fingerprint if rules_hash is None else rules_hash))

    def _reconcile_duplicates(self, db, code_keys=None):
        owners, changed = {}, set()
        query, params = 'SELECT * FROM reports', ()
        if code_keys is not None:
            code_keys = tuple(key for key in code_keys if key is not None)
            if not code_keys:
                return changed
            query += ' WHERE code_key IN (' + ','.join('?' for _ in code_keys) + ')'
            params = code_keys
        for row in db.execute(query + ' ORDER BY raw_message_id', params).fetchall():
            owner = None
            if row['base_status'] != 'INVALID' and row['code_key']:
                owner = owners.setdefault(row['code_key'], row['raw_message_id'])
                if owner == row['raw_message_id']:
                    owner = None
            status = 'DUPLICATE' if owner else row['base_status']
            if status != row['status'] or owner != row['duplicate_of']:
                db.execute('UPDATE reports SET status=?,duplicate_of=?,updated_at=CURRENT_TIMESTAMP WHERE raw_message_id=?',
                    (status, owner, row['raw_message_id']))
                changed.add(row['raw_message_id'])
                self._history(db, row['raw_message_id'], row['source_revision_id'], status,
                    json.loads(row['fields_json']), json.loads(row['issues_json']), owner, row['rules_hash'])
        return changed

    def replay_pending(self):
        with self.database.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM raw_messages WHERE processing_status='PENDING' ORDER BY id")]
        done = failed = 0
        for raw_id in ids:
            try:
                done += bool(self.process(raw_id))
            except Exception:
                failed += 1
        return {'processed': done, 'failed': failed}

    def reprocess(self, raw_id=None):
        # Mark first in a durable transaction so interruption can resume at startup.
        with self.database.connect() as db:
            if raw_id is None:
                count = db.execute("UPDATE raw_messages SET processing_status='PENDING'").rowcount
            else:
                count = db.execute("UPDATE raw_messages SET processing_status='PENDING' WHERE id=?", (raw_id,)).rowcount
        result = self.replay_pending()
        return {'queued': count, **result}
