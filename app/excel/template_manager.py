"""Load a fresh copy of an explicit, versioned workbook template per export."""
import json
from pathlib import Path
from openpyxl import load_workbook
from openpyxl.formula.translate import Translator
from openpyxl.utils.cell import column_index_from_string
import re
from .formatter import copy_row_style

ROOT = Path(__file__).resolve().parents[2]


class TemplateManager:
    def __init__(self, mapping_path=None):
        if mapping_path is None:
            local = ROOT / 'config' / 'excel_mapping.local.json'
            mapping_path = local if local.is_file() else ROOT / 'config' / 'excel_mapping.json'
        self.mapping_path = Path(mapping_path).resolve()
        self.mapping = json.loads(self.mapping_path.read_text(encoding='utf-8-sig'))
        self._validate_mapping()
        self.template_path = (self.mapping_path.parent / self.mapping['template']).resolve()

    def _validate_mapping(self):
        mapping = self.mapping
        if not isinstance(mapping, dict):
            raise ValueError('Excel mapping must be an object')
        if not isinstance(mapping.get('template'), str) or not mapping['template'].strip():
            raise ValueError('Excel template path is required')
        names = [mapping.get(key) for key in ('detail_sheet', 'summary_sheet', 'issues_sheet')]
        if any(not isinstance(name, str) or not name.strip() for name in names) or len(set(names)) != 3:
            raise ValueError('Excel worksheets must be distinct nonempty names')
        row = mapping.get('first_data_row')
        if type(row) is not int or not 5 <= row <= 1048576:
            raise ValueError('Excel first_data_row must be between 5 and 1048576')
        allowed = {'report_code', 'report_date', 'employee_id', 'employee_name',
                   'department', 'work_content', 'result', 'issue', 'group_name',
                   'chat_id', 'user_id', 'message_id', 'raw_message_id'}
        columns = mapping.get('columns')
        if not isinstance(columns, dict) or not columns or set(columns) - allowed:
            raise ValueError('Excel columns contain unknown fields or are empty')
        seen = set()
        for column in columns.values():
            if not isinstance(column, str) or not re.fullmatch(r'[A-Z]{1,3}', column):
                raise ValueError('Excel columns must be uppercase column letters')
            index = column_index_from_string(column)
            if index > 16384 or index in seen:
                raise ValueError('Excel columns must be unique and within A:XFD')
            seen.add(index)
        roster = mapping.get('roster_by_chat_id', {})
        if not isinstance(roster, dict):
            raise ValueError('Excel roster must be an object')
        for chat, users in roster.items():
            if not isinstance(chat, str) or not re.fullmatch(r'-?[1-9][0-9]*', chat):
                raise ValueError('Roster chat IDs must be canonical nonzero integer strings')
            if (not isinstance(users, list) or any(type(user) is not int or user <= 0 for user in users)
                    or len(set(users)) != len(users)):
                raise ValueError('Roster users must be unique positive integer IDs')

    def load(self):
        self._validate_mapping()
        book = load_workbook(self.template_path)
        for name in ('detail_sheet', 'summary_sheet', 'issues_sheet'):
            if self.mapping[name] not in book.sheetnames:
                book.close()
                raise ValueError('Template missing configured worksheet')
        try:
            detail = book[self.mapping['detail_sheet']]
            owned_columns = {column_index_from_string(c) for c in self.mapping['columns'].values()}
            # Reject ownership collisions, rather than silently destroy custom formulas.
            for sheet_name, cells in (
                (self.mapping['detail_sheet'], [detail['B2'], detail['B3']]),
                (self.mapping['summary_sheet'], [book[self.mapping['summary_sheet']].cell(r, c)
                                                 for r in range(3, 11) for c in (1, 2)]),
            ):
                if any(cell.data_type == 'f' or cell.__class__.__name__ == 'MergedCell' for cell in cells):
                    raise ValueError('Template formula or merged cell collides with export metadata')
            for sheet_name, first, columns in (
                (self.mapping['detail_sheet'], self.mapping['first_data_row'], owned_columns),
                (self.mapping['issues_sheet'], 3, set(range(1, 7))),
            ):
                sheet = book[sheet_name]
                for row in sheet.iter_rows(min_row=first):
                    if any(cell.column in columns and cell.data_type == 'f' for cell in row):
                        raise ValueError('Template formula collides with exported data')
                    if any(cell.column in columns and cell.value not in (None, '') for cell in row):
                        raise ValueError('Template nonempty cell collides with exported data')
                for merged in sheet.merged_cells.ranges:
                    if merged.max_row >= first and any(merged.min_col <= col <= merged.max_col for col in columns):
                        raise ValueError('Template merged cells collide with exported data')
        except Exception:
            book.close()
            raise
        return book

    def prepare_row(self, sheet, row):
        source = self.mapping['first_data_row']
        if row != source:
            copy_row_style(sheet, source, row)
            for cell in sheet[source]:
                if cell.data_type == 'f' and sheet.cell(row, cell.column).data_type != 'f':
                    sheet.cell(row, cell.column, Translator(cell.value, origin=cell.coordinate).translate_formula(sheet.cell(row, cell.column).coordinate))
