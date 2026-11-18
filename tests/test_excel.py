import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from aiogram.types import Chat, Message, Update, User
from openpyxl import load_workbook
from app.database.database import Database
from app.excel import ExportGenerator, TemplateManager
from app.excel.formatter import safe_text
from app.config import Settings
from app.bot.bot import create_dispatcher

DAY='2026-11-18'

@pytest.fixture
def db(tmp_path):
    database=Database(tmp_path/'test.db')
    database.register(-10,'../../same')
    database.register(-11,'../../same')
    database.register(-12,'Empty group')
    with database.connect() as c:
        for i,(chat,status,day) in enumerate([(-10,'VALID',DAY),(-10,'VALID',DAY),(-10,'INVALID',DAY),(-10,'DUPLICATE',DAY),(-11,'NEED_REVIEW',DAY),(-11,'VALID',DAY),(-11,'VALID','2026-11-17')],1):
            c.execute("INSERT INTO raw_messages(id,chat_id,message_id,user_id,submitted_at,message_json,processing_status) VALUES (?,?,?,?,?,'{}','PROCESSED')",(i,chat,i,42,DAY))
            fields={'report_code':f'BC-{i}','report_date':day,'employee_name':'=HYPERLINK("evil")','work_content':'Task\nline','issue':'@evil','result':'+cmd','employee_id':'001'}
            c.execute('INSERT INTO reports(raw_message_id,report_date,fields_json,base_status,status,issues_json,rules_hash) VALUES (?,?,?,?,?,?,?)',(i,day,json.dumps(fields),'VALID' if status=='DUPLICATE' else status,status,'["=danger"]','test'))
    return database


def test_values_style_formula_scopes_and_rerun(db,tmp_path):
    template=Path('templates/report_template.xlsx')
    original=hashlib.sha256(template.read_bytes()).hexdigest()
    service=ExportGenerator(db,tmp_path/'out')
    result=service.export(DAY,99)
    assert len(result)==4 and all(r['status']=='SUCCESS' for r in result)
    assert len({r['path'].name for r in result})==4
    assert all(r['path'].resolve().is_relative_to((tmp_path/'out').resolve()) for r in result)
    wb=load_workbook(result[0]['path']); s=wb['Reports']
    assert s.max_row==7
    assert s['F5'].value=='Task\nline'
    assert s['D5'].value.startswith("'=HYPERLINK") and s['D5'].data_type=='s'
    assert s['C5'].value=='001'
    assert s['N5'].value=='=LEN(F5)' and s['N7'].value=='=LEN(F7)'
    assert s['F5']._style==s['F6']._style and s['F6'].alignment.wrap_text
    assert s.freeze_panes=='A5' and 'A1:N1' in s.merged_cells
    assert wb['Summary']['B5'].value==3
    assert [wb['Summary'].cell(i,2).value for i in (6,7,8)]==[1,1,1]
    assert wb['Summary']['B10'].value.startswith('UNKNOWN')
    assert wb['Issues'].max_row==5
    wb.close()
    again=service.export(DAY,99)
    assert again[0]['path']!=result[0]['path']
    with _book(again[0]['path']) as w:
        assert w['Reports'].max_row==7
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM export_history WHERE status='SUCCESS'").fetchone()[0]==8
    assert hashlib.sha256(template.read_bytes()).hexdigest()==original

from contextlib import contextmanager
@contextmanager
def _book(path):
    book=load_workbook(path)
    try: yield book
    finally: book.close()


def test_known_roster_and_pending(db,tmp_path):
    manager=TemplateManager()
    manager.mapping['roster_by_chat_id']={'-10':[42,43],'-11':[42],'-12':[]}
    with db.connect() as c: c.execute("UPDATE raw_messages SET processing_status='PENDING' WHERE id=1")
    results=ExportGenerator(db,tmp_path/'out',manager).export(DAY)
    with _book(results[0]['path']) as w:
        assert w['Summary']['B10'].value==1
        assert w['Summary']['B9'].value==1
        assert w['Reports'].max_row==6


def test_failure_is_durable_no_partial_file(db,tmp_path,monkeypatch):
    import openpyxl.workbook.workbook
    def fail(self,path):
        Path(path).write_bytes(b'partial')
        raise OSError('sensitive path')
    monkeypatch.setattr(openpyxl.workbook.workbook.Workbook,'save',fail)
    result=ExportGenerator(db,tmp_path/'out').export(DAY)
    assert all(r['status']=='FAILED' and r['path'] is None for r in result)
    assert not list((tmp_path/'out').rglob('*.xlsx'))
    with db.connect() as c:
        assert [r['error'] for r in c.execute('SELECT error FROM export_history')]==['OSError']*4


@pytest.mark.parametrize('text',['=1+1','+cmd','-cmd','@SUM(A1)','\t=bad','\r\n=bad',' \x00=bad'])
def test_injection(text):
    assert safe_text(text).startswith("'")


def test_bad_date(db,tmp_path):
    with pytest.raises(ValueError): ExportGenerator(db,tmp_path/'out').export('../2026-11-18')
    assert not (tmp_path/'out').exists()


def test_export_private_admin_only(db,tmp_path,monkeypatch):
    import app.bot.bot as module
    calls=[]
    class FakeExporter:
        def __init__(self,database): pass
        def export(self,day,admin):
            calls.append((day,admin))
            return [{'status':'SUCCESS','path':tmp_path/'file.xlsx'}]
    monkeypatch.setattr(module,'ExportGenerator',FakeExporter)
    settings=Settings('123:fake',frozenset({99}),-10)
    dp=create_dispatcher(settings,db)
    bot=AsyncMock()
    async def run():
        for uid,(user,chat,kind,text) in enumerate([(1,1,'private','/export'),(99,-10,'supergroup','/export'),(99,99,'private','/export wrong'),(99,99,'private','/export 2026-11-18')],1):
            msg=Message(message_id=uid,date=datetime.now(timezone.utc),chat=Chat(id=chat,type=kind),from_user=User(id=user,is_bot=False,first_name='A'),text=text)
            await dp.feed_update(bot,Update(update_id=uid,message=msg))
    asyncio.run(run())
    assert calls==[(DAY,99)]
    # aiogram methods are sent through Bot.__call__; one document goes only to private admin.
    docs=[c.args[0] for c in bot.mock_calls if c.args and type(c.args[0]).__name__=='SendDocument']
    assert len(docs)==1 and docs[0].chat_id==99

def test_missing_template_records_each_failure(db,tmp_path):
    manager=TemplateManager()
    manager.template_path=tmp_path/'missing.xlsx'
    results=ExportGenerator(db,tmp_path/'out',manager).export(DAY)
    assert all(r['status']=='FAILED' for r in results)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM export_history WHERE status='FAILED' AND error='FileNotFoundError'").fetchone()[0]==4


def test_group_workbooks_are_isolated(db,tmp_path):
    results=ExportGenerator(db,tmp_path/'out').export(DAY)
    by_name={r['path'].name:r['path'] for r in results}
    group10=next(p for n,p in by_name.items() if n.endswith('_ID-10.xlsx'))
    group11=next(p for n,p in by_name.items() if n.endswith('_ID-11.xlsx'))
    empty=next(p for n,p in by_name.items() if n.endswith('_ID-12.xlsx'))
    with _book(group10) as w:
        assert w['Summary']['B5'].value==2
        assert w['Reports']['J5'].value=="'-10"
        assert w['Reports']['J6'].value=="'-10"
    with _book(group11) as w:
        assert w['Summary']['B5'].value==1
        assert w['Reports']['A5'].value=='BC-6'
    with _book(empty) as w:
        assert w['Summary']['B5'].value==0
        assert w['Reports']['A5'].value is None


def test_readable_safe_filenames(db, tmp_path):
    db.register(-10, 'Nhóm Kỹ thuật / Test:*?')
    results = ExportGenerator(db, tmp_path/'out').export(DAY)
    names = [r['path'].name for r in results]
    assert f'Bao_cao_Tong_hop_Cong_ty_{DAY}.xlsx' in names
    assert f'Bao_cao_Nhóm_Kỹ_thuật_Test_{DAY}_ID-10.xlsx' in names
    assert all(not any(c in n for c in '<>:"/\\|?*') for n in names)
