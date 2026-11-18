"""Excel formatting and formula-injection protection."""
from copy import copy


def safe_text(value):
    """Force untrusted values to literal strings, including control-prefix attacks."""
    if value is None:
        return ''
    text = str(value)
    # XML 1.0 cannot represent these characters.
    text = ''.join(c for c in text if c in '\t\n\r' or
                   0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or
                   0x10000 <= ord(c) <= 0x10FFFF)
    if text.lstrip().startswith(('=', '+', '-', '@')):
        text = "'" + text
    # Excel cells are limited to 32767 characters (including our prefix).
    return text[:32767]


def literal(cell, value):
    cell.value = safe_text(value)
    cell.data_type = 's'


def copy_row_style(sheet, source, target):
    sheet.row_dimensions[target].height = sheet.row_dimensions[source].height
    for cell in sheet[source]:
        dest = sheet.cell(target, cell.column)
        dest._style = copy(cell._style)
        dest.alignment = copy(cell.alignment)
        dest.protection = copy(cell.protection)
        if cell.number_format:
            dest.number_format = cell.number_format
