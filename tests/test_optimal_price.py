from fastapi.testclient import TestClient
from dori import ai
from dori.app import create_app
from tests.test_manual_price import seed


def test_optimal_price_is_kept_between_ours_and_theirs_and_saved(tmp_path, monkeypatch):
    async def fake(product, ours, competitors, low, high):
        assert (ours, [c['price'] for c in competitors], low, high) == (100, [60], 60, 100)
        return {'price': 5, 'reason': 'Ниже рынка.', 'model': 'test'}  # outside the range on purpose
    monkeypatch.setattr(ai, 'recommend_price', fake)
    app = create_app(str(tmp_path / 'db.sqlite3'))
    with TestClient(app) as client:
        run = seed(app.state.store)
        pid = client.get('/api/report', params={'run_id': run}).json()['products'][0]['product']['id']
        advice = client.post(f'/api/products/{pid}/optimal-price', params={'run_id': run}).json()
        assert advice['price'] == 100 and advice['low'] == 60 and advice['high'] == 100  # clamped to 60, rounded to 100 UZS
        entry = client.get('/api/report', params={'run_id': run}).json()['products'][0]
        assert entry['advice']['price'] == 100 and entry['advice']['reason'] == 'Ниже рынка.'
        assert 'optimal_price_uzs' in client.get('/api/export', params={'run_id': run}).text


def test_optimal_price_needs_confirmed_competitor_prices(tmp_path):
    app = create_app(str(tmp_path / 'db.sqlite3'))
    with TestClient(app) as client:
        run = seed(app.state.store)
        offer = client.get('/api/report', params={'run_id': run}).json()['products'][0]['offers'][0]
        client.post(f"/api/offers/{offer['id']}/review", json={'status': 'rejected'})
        pid = client.get('/api/report', params={'run_id': run}).json()['products'][0]['product']['id']
        assert client.post(f'/api/products/{pid}/optimal-price', params={'run_id': run}).status_code == 409


def test_optimal_price_without_our_excel_price_uses_competitor_range(tmp_path, monkeypatch):
    async def fake(product, ours, competitors, low, high):
        assert ours is None and (low, high) == (60, 60)
        return {'price': 59, 'reason': 'Нашей цены нет.', 'model': 'test'}
    monkeypatch.setattr(ai, 'recommend_price', fake)
    app = create_app(str(tmp_path / 'db.sqlite3'))
    with TestClient(app) as client:
        store = app.state.store
        ids = store.import_products([{'name': 'Example 20 mg tab №30', 'manufacturer': 'Example Pharma'}], 'no price')
        run = store.create_run(store.products(ids), sites=['arzon'])
        store.save_result(run, ids[0], 'arzon', {'status': 'ok'}, [dict(
            site='arzon', site_id='1', name='Example 20 mg tab №30', manufacturer='Example Pharma', price=60,
            currency='UZS', url='https://arzonapteka.uz/product/1', observed_at='2026-11-12T00:00:00Z')])
        advice = client.post(f'/api/products/{ids[0]}/optimal-price', params={'run_id': run}).json()
        assert advice['price'] == 60


def test_fit_price_rounds_without_leaving_the_range():
    from dori.app import fit_price
    assert fit_price(243180, 243180, 243180) == 243180      # single price: no rounding outside it
    assert fit_price(240000, 233000, 255000) == 240000
    assert fit_price(233050, 233010, 233090) == 233050      # range narrower than 100 UZS
    assert fit_price(1, 233000, 255000) == 233000
