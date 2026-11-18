"""Date-scoped snapshot exports; original templates and reports are never mutated."""
import json
import os
import re
import tempfile
import uuid
import unicodedata
from collections import Counter
from datetime import date
from pathlib import Path
from .formatter import literal
from .template_manager import ROOT, TemplateManager


class ExportGenerator:
    def __init__(self, database, output_dir=ROOT / 'data' / 'exports', template_manager=None):
        self.database = database
        self.output_dir = Path(output_dir)
        self.templates = template_manager or TemplateManager()

    def export(self, report_date, requested_by=None, group_ids=None):
        if date.fromisoformat(report_date).isoformat() != report_date:
            raise ValueError('Use YYYY-MM-DD')
        # A single read transaction gives every workbook the same snapshot.
        with self.database.connect() as conn:
            conn.execute('BEGIN')
            groups = [dict(r) for r in conn.execute('SELECT * FROM telegram_groups ORDER BY chat_id')]
            rows = [dict(r) for r in conn.execute('''SELECT r.*, m.chat_id,m.user_id,m.message_id,
                m.processing_status,g.display_name AS group_name FROM reports r
                JOIN raw_messages m ON m.id=r.raw_message_id JOIN telegram_groups g ON g.chat_id=m.chat_id
                WHERE r.report_date=? ORDER BY m.chat_id,r.raw_message_id''', (report_date,))]
        company_groups = groups
        if group_ids is not None:
            allowed = set(group_ids)
            groups = [g for g in groups if g["chat_id"] in allowed]
        run_id = uuid.uuid4().hex
        folder = self.output_dir / report_date / run_id
        scopes = [('Bao_cao_Tong_hop_Cong_ty', None, 'Company', rows)]
        for group in groups:
            chat_id = group['chat_id']
            # Preserve Vietnamese letters; strip Windows-invalid/path/control characters.
            normalized = unicodedata.normalize('NFC', group['display_name'])
            slug = re.sub(r'[^\w-]+', '_', normalized, flags=re.UNICODE).strip('_')[:60] or 'Nhom'
            scopes.append((f'Bao_cao_{slug}', chat_id, group['display_name'], [r for r in rows if r['chat_id'] == chat_id]))
        results = []
        for name, chat_id, title, subset in scopes:
            suffix = f'_ID{chat_id}' if chat_id is not None else ''
            tail = f'_{report_date}{suffix}.xlsx'
            # Windows MAX_PATH counts UTF-16 units, not Python characters.
            budget = min(255, 259 - len(str(folder.resolve()).encode('utf-16-le')) // 2 - 1)
            if chat_id is not None:
                name_budget = max(0, budget - len(tail.encode('utf-16-le')) // 2)
                name = name.encode('utf-16-le')[:name_budget * 2].decode('utf-16-le', errors='ignore')
            path = folder / f'{name}{tail}'
            with self.database.connect() as conn:
                history_id = conn.execute('''INSERT INTO export_history
                    (run_id,report_date,chat_id,requested_by,file_path,status) VALUES (?,?,?,?,?,'STARTED')''',
                    (run_id,report_date,chat_id,requested_by,str(path))).lastrowid
            try:
                if (len(str(path.resolve()).encode('utf-16-le')) // 2 > 259
                        or len(str(folder.resolve()).encode('utf-16-le')) // 2 + 22 > 259
                        or (chat_id is not None and len(name) < len('Bao_cao_'))):
                    raise ValueError('Export directory is too long for Windows paths')
                folder.mkdir(parents=True, exist_ok=True)
                self._workbook(path, report_date, title, subset, company_groups if chat_id is None else [g for g in groups if g['chat_id'] == chat_id])
            except Exception as exc:
                with self.database.connect() as conn:
                    conn.execute("UPDATE export_history SET status='FAILED',error=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (type(exc).__name__,history_id))
                results.append({'path': None, 'status': 'FAILED', 'history_id': history_id})
            else:
                with self.database.connect() as conn:
                    conn.execute("UPDATE export_history SET status='SUCCESS',completed_at=CURRENT_TIMESTAMP WHERE id=?", (history_id,))
                results.append({'path': path, 'status': 'SUCCESS', 'history_id': history_id})
        return results

    def _workbook(self, path, day, title, rows, groups):
        book = self.templates.load()
        temporary = None
        try:
            mapping = self.templates.mapping
            sheet = book[mapping['detail_sheet']]
            literal(sheet['B2'], title)
            literal(sheet['B3'], day)
            valid = [r for r in rows if r['status'] == 'VALID' and r['processing_status'] != 'PENDING']
            for index, row in enumerate(valid, mapping['first_data_row']):
                self.templates.prepare_row(sheet, index)
                fields = json.loads(row['fields_json'])
                fields.update({k: row[k] for k in ('group_name','chat_id','user_id','message_id','raw_message_id')})
                for field, column in mapping['columns'].items():
                    literal(sheet[f'{column}{index}'], fields.get(field, ''))
            if sheet.auto_filter.ref:
                from openpyxl.utils.cell import range_boundaries, get_column_letter
                left, top, right, _ = range_boundaries(sheet.auto_filter.ref)
                sheet.auto_filter.ref = f'{get_column_letter(left)}{top}:{get_column_letter(right)}{max(top, mapping["first_data_row"] + len(valid) - 1)}'
            summary = book[mapping['summary_sheet']]
            counts = Counter(r['status'] for r in rows if r['processing_status'] != 'PENDING')
            summary_rows = [('Report date',day),('Scope',title),('VALID',len(valid)),('INVALID',counts['INVALID']),
                ('DUPLICATE',counts['DUPLICATE']),('NEED_REVIEW',counts['NEED_REVIEW']),
                ('PENDING (excluded)',sum(r['processing_status']=='PENDING' for r in rows)),('Missing employees',self._missing(rows,groups))]
            for index, (label,value) in enumerate(summary_rows, 3):
                literal(summary.cell(index,1), label)
                if isinstance(value, int): summary.cell(index,2,value)
                else: literal(summary.cell(index,2),value)
            issues = book[mapping['issues_sheet']]
            for index, row in enumerate((r for r in rows if r['status'] != 'VALID' or r['processing_status']=='PENDING'), 3):
                for column, value in enumerate((row['raw_message_id'],row['chat_id'],row['message_id'], 'PENDING' if row['processing_status']=='PENDING' else row['status'],row['issues_json'],row['duplicate_of']),1):
                    literal(issues.cell(index,column),value)
            fd, temporary = tempfile.mkstemp(prefix='.export-',suffix='.xlsx',dir=path.parent)
            os.close(fd)
            book.save(temporary)
            os.replace(temporary,path)
            temporary = None
        finally:
            book.close()
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def _missing(self, rows, groups):
        roster = self.templates.mapping.get('roster_by_chat_id', {})
        if not groups or any(str(g['chat_id']) not in roster for g in groups):
            return 'UNKNOWN (no complete roster configured)'
        missing = 0
        for group in groups:
            expected = set(roster[str(group['chat_id'])])
            submitted = {r['user_id'] for r in rows if r['chat_id']==group['chat_id'] and r['status']=='VALID' and r['processing_status']!='PENDING'}
            missing += len(expected-submitted)
        return missing
