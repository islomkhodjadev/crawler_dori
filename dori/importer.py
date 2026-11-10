import csv
import io
import math
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

ALIASES = {
    'name': ['наименование товаров', 'наименование', 'название', 'product name', 'name', 'товар'],
    'manufacturer': ['производитель', 'manufacturer', 'завод'],
    'invoice_price': ['цена', 'price', 'invoice_price', 'purchase price'],
    'quantity': ['колич.', 'количество', 'quantity', 'qty'],
    'external_id': ['id', 'код', 'product_id', 'артикул'],
    'barcode': ['barcode', 'штрихкод', 'штрих код', 'ean'],
    'oson_id': ['oson_id'], 'arzon_id': ['arzon_id'],
}

def number(value):
    if value is None or str(value).strip() == '':
        return None
    try:
        n = Decimal(re.sub(r'[\s\u00a0]', '', str(value)).replace(',', '.'))
        return float(n) if n.is_finite() and n >= 0 else None
    except InvalidOperation:
        return None

def text(value):
    if isinstance(value, float):
        if not math.isfinite(value):
            return ''
        if value.is_integer():
            return str(int(value))
    return str(value or '').strip()

def read_products(data: bytes, filename: str):
    """Find tabular headers, ignoring invoice prose, merged blanks and totals."""
    suffix = Path(filename).suffix.lower()
    if suffix == '.xls':
        import xlrd
        book = xlrd.open_workbook(file_contents=data)
        sheets = [(s.name, [s.row_values(i) for i in range(s.nrows)]) for s in book.sheets()]
    elif suffix == '.xlsx':
        import openpyxl
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheets = [(s.title, list(s.values)) for s in book]
        book.close()
    elif suffix == '.csv':
        content = data.decode('utf-8-sig')
        try:
            dialect = csv.Sniffer().sniff(content[:8192], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        sheets = [('CSV', list(csv.reader(io.StringIO(content), dialect)))]
    else:
        raise ValueError('Use .xls, .xlsx or UTF-8 .csv')
    products = []
    for sheet, rows in sheets:
        columns = None
        for line, row in enumerate(rows, 1):
            values = [re.sub(r'\s+', ' ', text(v)).casefold() for v in row]
            detected = {key: next((i for i, v in enumerate(values) if v in aliases), None)
                        for key, aliases in ALIASES.items()}
            if detected['name'] is not None:
                columns = detected
                continue
            if not columns:
                continue
            def get(key):
                col = columns.get(key)
                return row[col] if col is not None and col < len(row) else None
            name = text(get('name'))
            if not name or re.match(r'^(итого|всего|total|сумма)\b', name, re.I):
                continue
            products.append(dict(name=name, manufacturer=text(get('manufacturer')),
                                 invoice_price=number(get('invoice_price')),
                                 quantity=number(get('quantity')), external_id=text(get('external_id')),
                                 barcode=text(get('barcode')), oson_id=text(get('oson_id')),
                                 arzon_id=text(get('arzon_id')), sheet=sheet, source_row=line))
    if not products:
        raise ValueError('No product rows found. Use a name / Наименование товаров header.')
    return products
