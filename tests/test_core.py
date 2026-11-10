import io
import json
import pytest
from fastapi.testclient import TestClient
from dori.importer import read_products, number
from dori.matching import match, attributes
from dori.estimate import estimate
from dori.adapters import parse_arzon, parse_oson_jsonld, price_value
from dori.storage import Store
from dori.app import create_app


def product(name='Тест 4000МЕ капс №30'):
    return {'name': name, 'manufacturer': 'Example Pharma, LTD', 'invoice_price': 60000,
            'quantity': 2, 'sheet': 'Invoice', 'source_row': 7}


def offer(name='Тест, 4000 МЕ, капс. №30', site='arzon', price=50000):
    return dict(name=name, manufacturer='Example Pharma', site=site, site_id='1',
                price=price, currency='UZS', url=f'https://{site}apteka.uz/product/1',
                observed_at='2026-11-10T00:00:00Z', match_status='accepted')


def test_csv_invoice_header_and_totals():
    content = 'Invoice 123;;;;;\nПоставщик;Some Company;;;;\n№;Наименование товаров;Колич.;Цена;Производитель;barcode\n1;Тест 20МГ №30;3;"12 000,50";Example;0012345678901\n;Итого;;36001,50;;'
    rows = read_products(content.encode(), 'a.csv')
    assert len(rows) == 1
    assert rows[0]['source_row'] == 4
    assert rows[0]['invoice_price'] == 12000.5
    assert rows[0]['barcode'] == '0012345678901'


def test_xlsx_merged_header():
    import openpyxl
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(['Invoice number 123'])
    sheet.merge_cells('A1:D1')
    sheet.append(['', 'Наименование товаров', '', 'Цена', 'Производитель'])
    sheet.append([1, 'Тест 20МГ №30', '', 23000, 'Example'])
    buf = io.BytesIO(); book.save(buf)
    rows = read_products(buf.getvalue(), 'a.xlsx')
    assert rows[0]['invoice_price'] == 23000
    assert rows[0]['source_row'] == 3


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-10', 'bad', ''])
def test_invalid_price(value):
    assert number(value) is None


def test_missing_header():
    with pytest.raises(ValueError):
        read_products(b'irrelevant,text\na,b', 'a.csv')


def test_correct_package():
    assert match(product(), offer())['status'] == 'accepted'


@pytest.mark.parametrize('name', ['Тест 2000МЕ капс №30', 'Тест 4000МЕ капс №60', 'Тест 4000МЕ таб №30'])
def test_reject_variant(name):
    assert match(product(), offer(name))['status'] == 'rejected'


def test_equivalent_units():
    assert attributes('Тест 0,05 г таб №20')['measures'] == attributes('Тест 50 мг таб №20')['measures']
    assert match(product('Тест 0,05 г таб №20'), offer('Тест 50 мг таб №20'))['status'] == 'accepted'


def test_grouped_thousands():
    assert attributes('NOW ВИТАМИН Д3 КАПС 5 000МЕ №120')['measures'] == [('iu','5000')]


def test_missing_identity_details_require_review():
    assert match(product(), offer('Тест 4000 МЕ капс'))['status'] == 'review'
    p = product(); p['manufacturer'] = ''
    assert match(p, offer())['status'] == 'review'


def test_similar_brand_requires_review():
    p = product('Тестолон 4000МЕ капс №30')
    assert match(p, offer('Тестолон-Н 4000МЕ капс №30'))['status'] != 'accepted'


def test_conflicting_manufacturer_and_ids():
    o = offer(); o['manufacturer'] = 'Other Corporation'
    assert match(product(), o)['status'] == 'rejected'
    p = product(); p['arzon_id'] = '99'
    assert match(p, offer())['status'] == 'rejected'
    p = product(); p['barcode'] = '123'
    o = offer(); o['barcode'] = '456'
    assert match(p, o)['status'] == 'rejected'


def test_estimator_balance_dedup_and_missing_source():
    a = offer(price=100)
    b = offer(site='oson', price=200)
    e = estimate([a,a,a,b], 50)
    assert e['value'] == 150
    assert e['accepted_listings'] == 2
    assert e['comparison_available']
    assert e['invoice_delta_percent'] == 200
    assert estimate([a])['status'] == 'single_source'
    assert estimate([])['value'] is None
    b['match_status'] = 'review'
    assert estimate([a,b])['value'] == 100


def test_arzon_parser_tied_to_card():
    html = '''<div>от 1 сўм advertisement</div><article data-product-card="detailed"><div>
    <p>Тест 20 мг таб №30</p><span><span>Example</span></span><p>Доступно в 10 аптеках</p>
    <p>от 53 400 сўм</p></div><a aria-label="Тест 20 мг таб №30" href="/ru/products/test-123"></a></article>'''
    offers = parse_arzon(html, 'https://arzonapteka.uz/ru/search?q=test')
    assert len(offers) == 1 and offers[0]['price'] == 53400
    assert offers[0]['manufacturer'] == 'Example'
    assert offers[0]['site_id'] == '123'
    assert price_value('Доступно в 100 аптеках') is None


def test_oson_jsonld_requires_uzs():
    data = {'@type':'Product','name':'Тест','offers':{'price':50,'priceCurrency':'USD'}}
    assert not parse_oson_jsonld('<script type="application/ld+json">'+json.dumps(data)+'</script>', 'https://osonapteka.uz/')
    data['offers']['priceCurrency'] = 'UZS'
    assert parse_oson_jsonld('<script type="application/ld+json">'+json.dumps(data)+'</script>', 'https://osonapteka.uz/')[0]['price'] == 50


def test_storage_run_history_and_review(tmp_path):
    store = Store(str(tmp_path/'db.sqlite3'))
    pid = store.import_products([product()], 'test')[0]
    r1 = store.create_run([product()])
    with pytest.raises(ValueError):
        store.create_run([product()])
    store.save_result(r1, pid, 'arzon', {'status':'ok'}, [offer()])
    report = store.report(r1)
    assert report['products'][0]['estimate']['value'] == 50000
    oid = report['products'][0]['offers'][0]['id']
    store.review(oid, 'rejected')
    assert store.report(r1)['products'][0]['estimate']['value'] is None
    store.update_run(r1, status='completed')
    r2 = store.create_run([product()])
    store.save_result(r2, pid, 'arzon', {'status':'error','error':'blocked'}, [])
    assert store.report(r2)['products'][0]['estimate']['value'] is None
    assert store.report(r1)['products'][0]['offers']


def test_hard_conflicts_cannot_be_manually_accepted(tmp_path):
    store=Store(str(tmp_path/'db.sqlite3'))
    pid=store.import_products([product()], 'test')[0]
    run=store.create_run([product()])
    store.save_result(run,pid,'arzon',{'status':'ok'},[offer('Тест 2000МЕ капс №30')])
    oid=store.report(run)['products'][0]['offers'][0]['id']
    with pytest.raises(ValueError):
        store.review(oid,'accepted')


def test_web_import_report_and_formula_safe_export(tmp_path):
    app=create_app(str(tmp_path/'db.sqlite3'))
    with TestClient(app) as client:
        assert client.get('/').status_code == 200
        assert client.get('/api/health').json()['ok']
        response=client.post('/api/import', files={'file':('test.csv', b'name,manufacturer,price\n=BAD,Example,12\n','text/csv')})
        assert response.status_code == 200
        pid=response.json()['product_ids'][0]
        store=app.state.store
        run=store.create_run(store.products())
        store.save_result(run,pid,'arzon',{'status':'error','error':'blocked'},[])
        store.update_run(run,status='completed')
        assert client.get('/api/report').json()['products'][0]['estimate']['value'] is None
        assert "'=BAD" in client.get('/api/export').text
        assert client.post('/api/products',json={'name':'new'},headers={'Origin':'https://evil.example'}).status_code == 403
        assert client.post('/api/products',json={'name':'new'}).status_code == 200


def test_ai_extraction_evidence_and_review():
    from dori.ai import ParsedPage, ParsedOffer, validate_visible_offers
    parsed=ParsedPage(offers=[ParsedOffer(name='Тест 4000МЕ капс №30',manufacturer='Example Pharma',price=53400,currency='UZS'),
                              ParsedOffer(name='Invented',manufacturer='Example Pharma',price=1,currency='UZS')])
    text='Тест 4000МЕ капс №30\nExample Pharma\nот 53 400 сўм'
    offers=validate_visible_offers(parsed,text,'https://osonapteka.uz/search','oson')
    assert len(offers)==1
    assert match(product(),offers[0])['status']=='review'
    with pytest.raises(ValueError):
        validate_visible_offers(parsed,text,'https://evil.example','oson')
