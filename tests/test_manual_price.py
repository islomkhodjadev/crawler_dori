from fastapi.testclient import TestClient
from dori.app import create_app


def seed(store):
    ids = store.import_products([{'name': 'Example 20 mg tab №30', 'manufacturer': 'Example Pharma', 'invoice_price': 100}], 'test')
    run = store.create_run(store.products(ids), sites=['arzon'])
    store.save_result(run, ids[0], 'arzon', {'status': 'ok'}, [dict(
        site='arzon', site_id='1', name='Example 20 mg tab №30', manufacturer='Example Pharma', price=60, currency='UZS',
        url='https://arzonapteka.uz/product/1', observed_at='2026-11-12T00:00:00Z')])
    return run


def test_manual_price_replaces_read_price_in_estimate_and_can_be_reset(tmp_path):
    app = create_app(str(tmp_path / 'db.sqlite3'))
    with TestClient(app) as client:
        run = seed(app.state.store)
        entry = client.get('/api/report', params={'run_id': run}).json()['products'][0]
        offer = entry['offers'][0]
        assert offer['match_status'] == 'accepted' and entry['estimate']['value'] == 60
        assert client.post(f"/api/offers/{offer['id']}/price", json={'price': 90}).status_code == 200
        entry = client.get('/api/report', params={'run_id': run}).json()['products'][0]
        offer = entry['offers'][0]
        assert offer['price'] == 90 and offer['observed_price'] == 60 and entry['estimate']['value'] == 90
        assert 'было 60 сум, стало 90 сум' in offer['comment']
        assert client.post(f"/api/offers/{offer['id']}/price", json={'price': None}).status_code == 200
        offer = client.get('/api/report', params={'run_id': run}).json()['products'][0]['offers'][0]
        assert offer['price'] == 60 and 'observed_price' not in offer


def test_manual_price_must_be_positive(tmp_path):
    app = create_app(str(tmp_path / 'db.sqlite3'))
    with TestClient(app) as client:
        run = seed(app.state.store)
        offer_id = client.get('/api/report', params={'run_id': run}).json()['products'][0]['offers'][0]['id']
        assert client.post(f'/api/offers/{offer_id}/price', json={'price': -5}).status_code == 422
        assert client.post('/api/offers/99999/price', json={'price': 5}).status_code == 404
