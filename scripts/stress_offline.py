"""Opt-in synthetic benchmark. Never loads .env, live data, or Telegram.

Run: python scripts/stress_offline.py --run [--export-rounds 3]
All mutable files live in a TemporaryDirectory and are removed on exit.
Measurements are observations, not throughput assertions or capacity guarantees.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import shutil
import tempfile
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiogram.types import Chat, Message, Update, User
from openpyxl import load_workbook
from app.database.database import Database
from app.excel import ExportGenerator
from app.excel.template_manager import TemplateManager
from app.parser import ReportParser, ReportProcessor


class MeasuredDatabase(Database):
    def __init__(self, path):
        self.scan_queries = {'duplicate_full_scan': 0, 'previous_projection_full_scan': 0,
                             'current_projection_full_scan': 0}
        super().__init__(path)

    @contextmanager
    def connect(self):
        with super().connect() as conn:
            def trace(sql):
                normalized = ' '.join(sql.lower().split())
                if normalized == 'select * from reports order by raw_message_id':
                    self.scan_queries['duplicate_full_scan'] += 1
                elif normalized == 'select * from report_export_state':
                    self.scan_queries['previous_projection_full_scan'] += 1
                elif normalized.startswith('select r.raw_message_id,r.report_date,r.fields_json,') and 'r.raw_message_id in (' not in normalized:
                    self.scan_queries['current_projection_full_scan'] += 1
            conn.set_trace_callback(trace)
            yield conn


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--run', action='store_true', help='Explicitly opt into the 1000-report benchmark')
    ap.add_argument('--export-rounds', type=int, default=1, help='Repeat full 21-workbook snapshot exports (1-10)')
    args = ap.parse_args()
    if not args.run:
        ap.error('benchmark disabled by default; pass --run to opt in')
    if not 1 <= args.export_rounds <= 10:
        ap.error('--export-rounds must be between 1 and 10')
    count = 1000
    groups = [-1000-i for i in range(20)]
    users = [10000+i for i in range(count)]  # 50 distinct, globally unique users per group
    with tempfile.TemporaryDirectory(prefix='collector-offline-') as folder:
        root = Path(folder)
        rules = root/'rules.json'
        rules.write_text(json.dumps({
            'labels': {'report_code':['Code'], 'report_date':['Date'],
                       'employee_name':['Employee'], 'work_content':['Work']},
            'required_fields':['report_code','report_date','employee_name','work_content'],
            'multiline_fields':['work_content'], 'date_formats':['%Y-%m-%d'],
            'timezone':'UTC', 'policies': {'employee_ids': users, 'group_ids': groups,
                                          'submission_hours':[0,23]}}), encoding='utf-8')
        db = MeasuredDatabase(root/'offline.db')
        for group in groups:
            db.register(group, f'Group {group}')
        processor = ReportProcessor(db, ReportParser(rules))
        updates = [Update(update_id=i+1, message=Message(message_id=i//20+1,
            date=datetime(2026,11,18,2,tzinfo=timezone.utc),
            chat=Chat(id=groups[i%20],type='supergroup'),
            from_user=User(id=users[i],is_bot=False,first_name=f'Person {i}'),
            text=f'Code: OFFLINE-{i:04d}\nDate: 2026-11-18\nEmployee: Person {i}\nWork: Synthetic report'))
            for i in range(count)]
        start = perf_counter()
        assert all(db.collect(item) for item in updates)
        ingest_seconds = perf_counter()-start
        blocks = []
        start = checkpoint = perf_counter()
        for i, item in enumerate(updates, 1):
            assert processor.process(db.raw_id_for_update(item.update_id))
            if i % 250 == 0:
                now = perf_counter(); blocks.append(round(now-checkpoint, 4)); checkpoint=now
        process_seconds = perf_counter()-start
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM reports WHERE status='VALID'").fetchone()[0] == count
            assert conn.execute("SELECT COUNT(*) FROM raw_messages WHERE processing_status='PENDING'").fetchone()[0] == 0
        scan_counts = dict(db.scan_queries)
        # Copy the shipped presentation template, but never consult production mapping/roster.
        shutil.copyfile(Path(__file__).resolve().parents[1]/'templates'/'report_template.xlsx', root/'template.xlsx')
        mapping = root/'mapping.json'
        mapping.write_text(json.dumps({'version': 1, 'template': 'template.xlsx',
            'detail_sheet': 'Reports', 'summary_sheet': 'Summary', 'issues_sheet': 'Issues',
            'first_data_row': 5, 'columns': dict(zip(
                ['report_code','report_date','employee_id','employee_name','department',
                 'work_content','result','issue','group_name','chat_id','user_id','message_id','raw_message_id'],
                'ABCDEFGHIJKLM')), 'roster_by_chat_id': {}}), encoding='utf-8')
        exporter = ExportGenerator(db, root/'exports', TemplateManager(mapping))
        export_runs = []
        seen_paths = set()
        for _ in range(args.export_rounds):
            start = perf_counter(); results = exporter.export('2026-11-18')
            elapsed = perf_counter()-start
            assert len(results) == 21 and all(r['status']=='SUCCESS' for r in results)
            paths = {r['path'] for r in results}
            assert len(paths) == 21 and not paths.intersection(seen_paths)
            seen_paths.update(paths)
            exported_rows = []
            for result in results:
                book = load_workbook(result['path'], read_only=True, data_only=True)
                try:
                    exported_rows.append(sum(1 for row in book['Reports'].iter_rows(min_row=5, max_col=1, values_only=True)
                                             if row[0] is not None))
                finally:
                    book.close()
            assert exported_rows == [1000] + [50]*20
            export_runs.append({'seconds': round(elapsed, 4), 'workbooks': len(results),
                                'verified_company_rows': exported_rows[0],
                                'verified_rows_per_group': 50,
                                'bytes': sum(r['path'].stat().st_size for r in results)})
        print(json.dumps({'offline': True, 'temporary_files_removed_on_exit': True,
            'reports': count, 'groups': 20, 'distinct_users_per_group': 50,
            'ingest_seconds': round(ingest_seconds, 4), 'process_seconds': round(process_seconds, 4),
            'process_250_report_blocks_seconds': blocks, 'observed_full_scan_queries': scan_counts,
            'legacy_full_scan_rows_estimate_if_unoptimized': count*(count+1)//2,
            'scan_evidence': 'Trace counts full queries only; scoped IN queries are excluded. The legacy row estimate is a comparison baseline, not measured current visits.',
            'exports': export_runs}, indent=2))


if __name__ == '__main__':
    main()


