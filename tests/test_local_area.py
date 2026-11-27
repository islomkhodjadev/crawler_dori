import asyncio

import pytest

from dori.adapters import parse_arzon_pharmacies, parse_oson_pharmacies
from dori.agent import AgentBus, AgentInfo
from dori.service import crawl
from dori.storage import Store


@pytest.mark.anyio
async def test_branch_runs_use_only_assigned_agents_and_local_prices(tmp_path, monkeypatch):
    store = Store(str(tmp_path / 'branches.sqlite3'))
    ids = store.import_products([
        {'name': 'Example 20 mg tab №30', 'manufacturer': 'Example Pharma', 'invoice_price': 30000},
        {'name': 'Other 10 mg tab №10', 'manufacturer': 'Example Pharma', 'invoice_price': 30000},
    ], 'TEST')
    products = store.products(ids)
    branch_a = {'store_id': 1, 'store_name': 'A', 'lat': 41.3, 'lng': 69.25, 'radius_km': 3}
    branch_b = {'store_id': 2, 'store_name': 'B', 'lat': 41.5, 'lng': 69.25, 'radius_km': 3}
    first = [{**p, 'invoice_price': 11000} for p in products]
    second = [{**products[0], 'invoice_price': 21000}]
    run_a, run_b = store.create_branch_runs([(branch_a, first), (branch_b, second)], ['oson'])
    bus = AgentBus()
    socket_a1, socket_a2, socket_b = object(), object(), object()
    for ws, name, branch, lat in ((socket_a1, 'A1', 1, None), (socket_a2, 'A2', 1, 41.4), (socket_b, 'B', 2, None)):
        bus._agents[ws] = AgentInfo(name, name)
        store.assign_agent(name, branch, lat, 69.25 if lat else None)

    calls = []

    async def search(agent, product, **kwargs):
        calls.append((agent._ws, product['id']))
        return [
            {'site': 'oson', 'price_kind': 'per_pharmacy', 'name': product['name'],
             'manufacturer': product['manufacturer'], 'price': price, 'currency': 'UZS',
             'pharmacy_name': f'Pharmacy {lat}', 'latitude': lat, 'longitude': 69.25,
             'url': 'https://osonapteka.uz/product/example'}
            for lat, price in [(41.3, 10000), (41.301, 12000), (41.4, 15000), (41.5, 20000), (None, 5000)]
        ], {'status': 'ok'}

    monkeypatch.setattr('dori.adapters.agent_search_oson', search)
    admin = object()
    await asyncio.gather(
        crawl(store, run_a, first, ('oson',), admin=admin, agent_bus=bus, branch=branch_a),
        crawl(store, run_b, second, ('oson',), admin=admin, agent_bus=bus, branch=branch_b),
    )

    assert [(ws, pid) for ws, pid in calls if ws is socket_b] == [(socket_b, ids[0])]
    assert {ws for ws, _ in calls} == {socket_a1, socket_a2, socket_b}
    report_a, report_b = store.report(run_a), store.report(run_b)
    assert report_a['branch']['store_name'] == 'A'
    assert {entry['estimate']['value'] for entry in report_a['products']} == {11000, 15000}
    assert {entry['product']['invoice_price'] for entry in report_a['products']} == {11000}
    assert len(report_b['products']) == 1
    entry = report_b['products'][0]
    assert entry['estimate']['value'] == 20000
    assert entry['product']['invoice_price'] == 21000
    assert {o['location_status'] for o in entry['offers']} == {'nearby', 'outside', 'unknown'}


def test_source_coordinates_are_attached_to_pharmacies():
    html = '<p>Branch</p><p data-latitude="41.3" data-longitude="69.25"><span>Адрес:</span> Street</p><p>Product</p><p>1 шт × 12 700 сўм</p>'
    arzon = parse_arzon_pharmacies(html, 'Product', 'https://arzonapteka.uz/products/example')[0]
    assert (arzon['latitude'], arzon['longitude']) == (41.3, 69.25)
    json = '{"posList":[{"name":"Branch","address":"Street","lat":41.4,"lon":69.25,"minPrice":15000}]}'
    oson = parse_oson_pharmacies(json, 'Product', 'https://osonapteka.uz/product/example')[0]
    assert (oson['latitude'], oson['longitude']) == (41.4, 69.25)


def test_admin_starts_branch_collection_with_its_synced_stock(tmp_path, monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from dori.app import create_app

    bus = AgentBus()
    bus._agents[object()] = AgentInfo('chrome-a', 'Chrome A')
    monkeypatch.setattr('dori.app.AgentBus', lambda: bus)
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'collection-test-password')

    async def search(*args, **kwargs):
        return [], {'status': 'no_results'}

    monkeypatch.setattr('dori.adapters.agent_search_oson', search)
    app = create_app(str(tmp_path / 'branch-api.sqlite3'))
    products = [
        {'name': 'Stocked', 'dorixona_product_id': 101, 'invoice_price': 30000},
        {'name': 'Other branch', 'dorixona_product_id': 102, 'invoice_price': 30000},
    ]
    stock = [
        {'store_id': store_id, 'store_name': f'Branch {store_id}', 'store_address': '', 'lat': 41.3,
         'lng': 69.25, 'dorixona_product_id': item['dorixona_product_id'], 'name': item['name'],
         'qty': 3, 'price': 11000}
        for store_id, item in enumerate(products, start=1)
    ]
    ids = app.state.store.import_from_dorixona(products, stock, '2026-11-27')
    with TestClient(app) as client:
        assert client.post('/api/login', data={'username': 'admin', 'password': 'collection-test-password'},
                           follow_redirects=False).status_code == 303
        assert client.put('/api/agents/chrome-a/assignment', json={'store_id': 1}).status_code == 200
        assert client.get('/api/agents').json()[0]['assignment']['store_id'] == 1
        started = client.post('/api/crawl', json={'product_ids': ids, 'sites': ['oson'], 'store_ids': [1, 2]})
        assert started.status_code == 200
        run_a, run_b = started.json()['run_ids']
        for _ in range(40):
            runs = {run['id']: run for run in client.get('/api/runs').json()}
            if runs[run_a]['status'] == 'completed':
                break
            time.sleep(0.025)
        assert runs[run_a]['status'] == 'completed'
        assert runs[run_b]['status'] == 'queued'
        assert runs[run_a]['total'] == runs[run_b]['total'] == 1
        report = client.get('/api/report', params={'run_id': run_a}).json()
        assert report['branch']['store_id'] == 1
        assert report['products'][0]['product']['invoice_price'] == 11000
        assert client.post(f'/api/runs/{run_b}/cancel').status_code == 200


def test_extension_loads_branch_choices_and_saves_setup_without_admin_session(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from dori.app import create_app

    monkeypatch.setenv('DORI_AGENT_PASSWORD', 'extension-test-password')
    app = create_app(str(tmp_path / 'extension.sqlite3'))
    product = {'name': 'Stocked', 'dorixona_product_id': 101}
    app.state.store.import_from_dorixona([product], [
        {'store_id': 7, 'store_name': 'Branch Seven', 'store_address': 'Example street',
         'lat': 41.3, 'lng': 69.25, 'dorixona_product_id': 101, 'name': 'Stocked', 'qty': 2},
    ], '2026-11-27')
    agent_id = 'b7b7f059-390b-467e-8bb2-2f8fe248dd95'
    with TestClient(app) as client:
        path = '/api/agent/setup'
        extension = {'Origin': 'chrome-extension://example'}
        assert client.post(path, json={'password': 'incorrect'}, headers=extension).status_code == 401
        auth = {'password': 'extension-test-password'}
        assert client.post(path, json=auth, headers={'Origin': 'https://unrelated.example'}).status_code == 403
        response = client.post(path, json=auth, headers=extension)
        assert response.json() == {'items': [
            {'store_id': 7, 'store_name': 'Branch Seven', 'store_address': 'Example street'},
        ]}
        assert client.post(path, json={**auth, 'agent_id': agent_id, 'store_id': 999},
                           headers=extension).status_code == 404
        assert client.post(path, json={**auth, 'agent_id': agent_id, 'store_id': 7},
                           headers=extension).status_code == 200
        assert app.state.store.agent_assignments()[agent_id]['store_id'] == 7
        assert client.get('/api/agents', headers=extension).status_code == 401
