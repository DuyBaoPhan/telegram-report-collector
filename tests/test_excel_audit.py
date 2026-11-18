import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from app.database.database import Database
from app.excel import ExportGenerator, TemplateManager
from app.excel.formatter import safe_text


@pytest.fixture
def custom_template(tmp_path):
    mapping = json.loads(Path('config/excel_mapping.json').read_text(encoding='utf-8-sig'))
    template = tmp_path / 'template.xlsx'
    template.write_bytes(Path('templates/report_template.xlsx').read_bytes())
    mapping['template'] = 'template.xlsx'
    config = tmp_path / 'mapping.json'
    config.write_text(json.dumps(mapping))
    return config, template, mapping


def test_xml_invalid_unicode_removed_before_workbook_save(tmp_path):
    from openpyxl import Workbook
    from app.excel.formatter import literal
    value = 'Vietnamese Việt\ud800\udfff\ufffe\uffff\x00\t\n😀'
    assert safe_text(value) == 'Vietnamese Việt\t\n😀'
    book = Workbook()
    literal(book.active['A1'], value)
    path = tmp_path / 'unicode.xlsx'
    book.save(path)
    loaded = load_workbook(path)
    assert loaded.active['A1'].value == 'Vietnamese Việt\t\n😀'
    loaded.close()
    book.close()


@pytest.mark.parametrize('key,value', [
    ('first_data_row', 3), ('first_data_row', True), ('first_data_row', 1048577),
    ('columns', {'employee_nmae': 'A'}), ('columns', {'employee_name': 'A', 'result': 'A'}),
    ('columns', {'employee_name': 'XFE'}), ('columns', {'employee_name': 'a'}),
    ('columns', {'employee_name': 'A1'}), ('summary_sheet', 'Reports'),
    ('roster_by_chat_id', []), ('roster_by_chat_id', {'-1': '42'}),
    ('roster_by_chat_id', {'-1': ['42']}), ('roster_by_chat_id', {'-1': [True]}),
    ('roster_by_chat_id', {'-1': [42,42]}), ('roster_by_chat_id', {'-01': [42]}),
])
def test_invalid_mapping_fails_early(custom_template, key, value):
    config, _, mapping = custom_template
    mapping[key] = value
    config.write_text(json.dumps(mapping))
    with pytest.raises(ValueError):
        TemplateManager(config)


@pytest.mark.parametrize('sheet,cell', [('Reports','A5'), ('Reports','B2'),
                                        ('Summary','B5'), ('Issues','A3')])
def test_template_formula_collision_fails_without_modifying_source(custom_template, sheet, cell):
    config, template, _ = custom_template
    book = load_workbook(template)
    book[sheet][cell] = '=1+1'
    book.save(template)
    book.close()
    before = template.read_bytes()
    with pytest.raises(ValueError, match='formula'):
        TemplateManager(config).load()
    assert template.read_bytes() == before


def test_existing_unmapped_formula_preserved_and_new_rows_translated(custom_template):
    config, template, _ = custom_template
    book = load_workbook(template)
    book['Reports']['N6'] = '=LEN(F6)+1'
    book.save(template)
    book.close()
    manager = TemplateManager(config)
    book = manager.load()
    manager.prepare_row(book['Reports'], 6)
    manager.prepare_row(book['Reports'], 7)
    assert book['Reports']['N6'].value == '=LEN(F6)+1'
    assert book['Reports']['N7'].value == '=LEN(F7)'
    book.close()


def test_merged_data_cells_rejected(custom_template):
    config, template, _ = custom_template
    book = load_workbook(template)
    book['Reports'].merge_cells('A6:B6')
    book.save(template)
    book.close()
    with pytest.raises(ValueError, match='merged'):
        TemplateManager(config).load()


def test_malformed_roster_mutation_rejected_when_loading():
    manager = TemplateManager()
    manager.mapping['roster_by_chat_id'] = {'-1': ['42']}
    with pytest.raises(ValueError, match='Roster'):
        manager.load()


def test_path_budget_truncates_group_label_without_losing_identity(tmp_path):
    database = Database(tmp_path / 'db.sqlite')
    database.register(-123, 'Việt' * 50)
    # A normal directory can fit while the full 60-character label cannot.
    output = tmp_path / ('a' * max(1, 140 - len(str(tmp_path))))
    results = ExportGenerator(database, output).export('2026-11-18')
    assert all(result['status'] == 'SUCCESS' for result in results)
    group_path = results[1]['path']
    assert group_path.name.endswith('_2026-11-18_ID-123.xlsx')
    assert len(str(group_path.resolve()).encode('utf-16-le')) // 2 <= 259


def test_overlong_output_fails_durably_before_creating_directory(tmp_path):
    database = Database(tmp_path / 'db.sqlite')
    output = tmp_path / ('a' * 170)
    results = ExportGenerator(database, output).export('2026-11-18')
    assert results[0]['status'] == 'FAILED'
    assert not output.exists()
    with database.connect() as conn:
        assert conn.execute('SELECT error FROM export_history').fetchone()[0] == 'ValueError'


@pytest.mark.parametrize('sheet,cell,value', [
    ('Reports', 'A5', 'stale report'), ('Reports', 'M25', 0),
    ('Issues', 'A3', 'stale issue'), ('Issues', 'F25', False),
])
def test_template_data_constants_rejected_without_modifying_source(custom_template, sheet, cell, value):
    config, template, _ = custom_template
    book = load_workbook(template)
    book[sheet][cell] = value
    book.save(template)
    book.close()
    before = template.read_bytes()
    with pytest.raises(ValueError, match='nonempty'):
        TemplateManager(config).load()
    assert template.read_bytes() == before


@pytest.mark.parametrize('stale_sheets', [('Reports',), ('Issues',), ('Reports', 'Issues'), ()])
def test_empty_database_schedule_never_sends_stale_template_data(tmp_path, custom_template, stale_sheets):
    import asyncio
    from test_scheduler import service
    config, template, _ = custom_template
    book = load_workbook(template)
    for sheet in stale_sheets:
        book[sheet]['A5' if sheet == 'Reports' else 'A3'] = 'STALE PRIVATE DATA'
    # User formulas outside generator-owned columns and metadata labels are allowed.
    book['Reports']['N6'] = '=LEN(F6)+1'
    book.save(template)
    book.close()
    before = template.read_bytes()
    scheduled = service(tmp_path)
    scheduled.generator = ExportGenerator(scheduled.database, tmp_path/'out', TemplateManager(config))
    assert asyncio.run(scheduled.run()) == ('PARTIAL' if stale_sheets else 'SUCCESS')
    with scheduled.database.connect() as conn:
        history = conn.execute('SELECT status,error FROM export_history').fetchall()
        assert len(history) == 2  # company and registered group, even with no reports
        assert {row['status'] for row in history} == ({'FAILED'} if stale_sheets else {'SUCCESS'})
        if stale_sheets:
            assert all(row['error'] == 'ValueError' for row in history)
    if stale_sheets:
        scheduled.bot.send_document.assert_not_awaited()
        assert not list((tmp_path/'out').rglob('*.xlsx'))
    else:
        assert scheduled.bot.send_document.await_count == 4  # two recipients
        for path in (tmp_path/'out').rglob('*.xlsx'):
            exported = load_workbook(path)
            assert exported['Reports']['A5'].value is None
            assert exported['Issues']['A3'].value is None
            assert exported['Reports']['N6'].value == '=LEN(F6)+1'
            exported.close()
    assert template.read_bytes() == before
