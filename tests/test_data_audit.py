"""Offline data audit regressions; uses temporary databases only."""
import json

import pytest

from app.database.database import Database
from app.parser import ReportParser, ReportProcessor
from test_phase6 import rules_file, update


def text(code='A', day='2026-11-18'):
    return f'Code: {code}\nDate: {day}\nEmployee: Offline\nWork: Done'


def service(tmp_path):
    db = Database(tmp_path/'audit.db')
    db.register(-10, 'Offline')
    parser = ReportParser(rules_file(tmp_path/'rules.json'))
    return db, ReportProcessor(db, parser)


def collect(db, proc, uid, content, mid=None, edit=False):
    item = update(uid, mid=mid, text=content, edit=edit)
    assert db.collect(item)
    raw_id = db.raw_id_for_update(item)
    assert proc.process(raw_id)
    return raw_id


def test_duplicate_promotion_retains_original_rules_provenance(tmp_path):
    db, proc = service(tmp_path)
    first = collect(db, proc, 1, text())
    second = collect(db, proc, 2, text())
    original_hash = proc.parser.fingerprint
    # Different parser rules, only owner is reprocessed. The promoted duplicate
    # retains its existing parse/hash rather than claiming evaluation by new rules.
    proc.parser.fingerprint = 'new-rules'
    collect(db, proc, 3, 'ordinary chat', mid=1, edit=True)
    with db.connect() as conn:
        row = conn.execute('SELECT * FROM reports WHERE raw_message_id=?', (second,)).fetchone()
        hist = conn.execute('SELECT * FROM report_history WHERE raw_message_id=? ORDER BY id DESC', (second,)).fetchone()
        assert row['status'] == hist['status'] == 'VALID'
        assert row['rules_hash'] == hist['rules_hash'] == original_hash
        assert conn.execute('SELECT COUNT(*) FROM reports WHERE raw_message_id=?', (first,)).fetchone()[0] == 0


def test_scoped_projection_updates_leave_unrelated_rows_untouched(tmp_path):
    db, proc = service(tmp_path)
    first = collect(db, proc, 1, text('A'))
    second = collect(db, proc, 2, text('B'))
    with db.connect() as conn:
        conn.execute('CREATE TABLE writes(action TEXT, raw_id INTEGER)')
        conn.execute('CREATE TRIGGER state_delete AFTER DELETE ON report_export_state BEGIN INSERT INTO writes VALUES (\'delete\', old.raw_message_id); END')
        conn.execute('CREATE TRIGGER state_insert AFTER INSERT ON report_export_state BEGIN INSERT INTO writes VALUES (\'insert\', new.raw_message_id); END')
    collect(db, proc, 3, text('A', '2026-11-19'), mid=1, edit=True)
    with db.connect() as conn:
        assert {r[0] for r in conn.execute('SELECT raw_id FROM writes')} == {first}
        assert conn.execute('SELECT report_date FROM report_export_state WHERE raw_message_id=?', (second,)).fetchone()[0] == '2026-11-18'
        assert {r[0] for r in conn.execute('SELECT report_date FROM realtime_dirty')} == {'2026-11-18', '2026-11-19'}


def test_code_change_reconciles_both_families_and_dirty_days(tmp_path):
    db, proc = service(tmp_path)
    a = collect(db, proc, 1, text('A'))
    b = collect(db, proc, 2, text('A'))
    c = collect(db, proc, 3, text('B', '2026-11-19'))
    collect(db, proc, 4, text('B', '2026-11-20'), mid=1, edit=True)
    with db.connect() as conn:
        states = {r['raw_message_id']: (r['status'], r['duplicate_of']) for r in conn.execute('SELECT * FROM reports')}
        assert states == {a: ('VALID', None), b: ('VALID', None), c: ('DUPLICATE', a)}
        assert {r[0] for r in conn.execute('SELECT raw_message_id FROM report_export_state')} == {a, b}
        assert {r[0] for r in conn.execute('SELECT report_date FROM realtime_dirty')} == {'2026-11-18', '2026-11-19', '2026-11-20'}


@pytest.mark.parametrize('change', [
    {'required_fields': []}, {'required_fields': ['report_code']},
    {'required_fields': ['report_code', 'report_date', []]},
    {'multiline_fields': 'work_content'}, {'multiline_fields': ['unknown']},
])
def test_unsafe_rule_shapes_fail_at_startup(tmp_path, change):
    path = rules_file(tmp_path/'rules.json')
    config = json.loads(path.read_text())
    config.update(change)
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        ReportParser(path)


def test_scoped_updates_match_full_reconciliation_after_mixed_edits(tmp_path):
    db, proc = service(tmp_path)
    for uid in range(1, 25):
        collect(db, proc, uid, text(f'C{uid % 5}', f'2026-11-{18 + uid % 3}'))
    for uid in range(25, 49):
        content = ('ordinary' if uid % 4 == 0 else
                   text(f'C{uid % 7}', 'not-a-date' if uid % 6 == 0 else f'2026-11-{18 + uid % 3}'))
        collect(db, proc, uid, content, mid=uid - 24, edit=True)
        with db.connect() as conn:
            states_before = [tuple(r) for r in conn.execute('SELECT * FROM report_export_state ORDER BY raw_message_id')]
            dirty_before = [tuple(r) for r in conn.execute('SELECT * FROM realtime_dirty ORDER BY report_date,chat_id')]
            history_count = conn.execute('SELECT COUNT(*) FROM report_history').fetchone()[0]
            assert not proc._reconcile_duplicates(conn)
            assert not db.record_export_changes(conn)
            assert states_before == [tuple(r) for r in conn.execute('SELECT * FROM report_export_state ORDER BY raw_message_id')]
            assert dirty_before == [tuple(r) for r in conn.execute('SELECT * FROM realtime_dirty ORDER BY report_date,chat_id')]
            assert history_count == conn.execute('SELECT COUNT(*) FROM report_history').fetchone()[0]
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('formats', [['%%Y-%m-%d'], ['%Y-%%m-%d'], ['%Y-%m-%d %Q']])
def test_literal_or_unsupported_date_directives_rejected(tmp_path, formats):
    path = rules_file(tmp_path/'rules.json')
    config = json.loads(path.read_text())
    config['date_formats'] = formats
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        ReportParser(path)


def test_large_projection_family_uses_safe_unscoped_fallback(tmp_path):
    db, _ = service(tmp_path)
    # More than the explicit scoped-query budget, without relying on a particular
    # SQLite build's maximum variable number.
    with db.connect() as conn:
        conn.executemany('INSERT INTO report_export_state VALUES (?,?,?,?)',
                         [(i, '2026-11-18', -10, '[]') for i in range(1, 602)])
        assert db.record_export_changes(conn, raw_ids=range(1, 602)) == {('2026-11-18', -10)}
        assert conn.execute('SELECT COUNT(*) FROM report_export_state').fetchone()[0] == 0
        assert conn.execute('SELECT revision FROM realtime_dirty').fetchone()[0] == 1
