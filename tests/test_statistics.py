import csv
import io
import sqlite3
from fastapi.testclient import TestClient
from dori.app import create_app
from dori.storage import Store


def seed(store):
    products=[{'name':f'Example{i} 20 mg tab №30','manufacturer':'Example Pharma','invoice_price':100} for i in range(1,4)]
    ids=store.import_products(products,'Synthetic statistics fixture')
    rid=store.create_run(store.products(ids),sites=['arzon','oson'])
    def offer(pid,site,**extra):
        p=store.products([pid])[0]
        value=dict(site=site,site_id='1',name=p['name'],manufacturer=p['manufacturer'],price=90,currency='UZS',
                   url=f'https://{site}apteka.uz/product/{pid}',observed_at='2026-11-10T00:00:00Z')
        value.update(extra)
        return value
    store.save_result(rid,ids[0],'arzon',{'status':'ok'},[offer(ids[0],'arzon'),offer(ids[0],'arzon',name='Example1 40 mg tab №30',url='https://arzonapteka.uz/product/rejected')])
    store.save_result(rid,ids[0],'oson',{'status':'ok','extraction_method':'openai'},[offer(ids[0],'oson',extraction='ai_requires_review')])
    store.save_result(rid,ids[1],'arzon',{'status':'ok'},[offer(ids[1],'arzon',manufacturer='')])
    store.save_result(rid,ids[1],'oson',{'status':'no_results'},[])
    store.save_result(rid,ids[2],'arzon',{'status':'error','error':'Network failed'},[])
    store.update_run(rid,status='running',completed=2,active_product_id=ids[2],active_site='oson',phase='Searching and extracting prices')
    return rid,ids


def test_counts_distinguish_products_listings_and_ok_sources(tmp_path):
    store=Store(str(tmp_path/'db.sqlite3'))
    rid,ids=seed(store)
    data=store.statistics(rid)
    assert data['products']=={'selected':3,'completed':2,'remaining':1,'matched':1,'review_only':1,
                              'completed_without_match':1,'two_source_matches':0}
    assert data['listings']=={'total':4,'accepted':1,'review':2,'rejected':1}
    assert data['progress_percent']==66.7
    assert data['active']['id']==ids[2] and data['active']['site']=='oson'
    sources={s['site']:s for s in data['sources']}
    assert sources['arzon']['successful']==2 and sources['arzon']['errors']==1
    assert sources['oson']['successful']==1 and sources['oson']['accepted']==0
    assert sources['oson']['review']==1 and sources['oson']['no_results']==1
    assert data['events'][0]['message']=='Network failed'


def test_manual_review_changes_statistics_without_new_crawl(tmp_path):
    store=Store(str(tmp_path/'db.sqlite3'))
    rid,ids=seed(store)
    first=store.report(rid)['products'][0]
    oid=next(o['id'] for o in first['offers'] if o['site']=='oson')
    store.review(oid,'accepted')
    data=store.statistics(rid)
    assert data['listings']['accepted']==2 and data['listings']['review']==1
    assert data['products']['matched']==1 and data['products']['two_source_matches']==1


def test_empty_statistics_and_run_isolation(tmp_path):
    store=Store(str(tmp_path/'db.sqlite3'))
    assert store.statistics()['listings']['total']==0
    rid,ids=seed(store)
    store.update_run(rid,status='completed',completed=3)
    newer=store.create_run(store.products(ids[:1]),sites=['arzon'])
    assert store.statistics()['run_id']==newer
    assert store.statistics()['listings']['total']==0
    assert store.statistics(rid)['listings']['total']==4


def test_old_database_migration_preserves_runs(tmp_path):
    path=str(tmp_path/'old.sqlite3')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE runs (id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,total INTEGER NOT NULL,completed INTEGER NOT NULL DEFAULT 0,error TEXT)')
        db.execute("INSERT INTO runs VALUES (1,'2026-11-10T00:00:00Z',NULL,'completed',10,10,NULL)")
    store=Store(path)
    row=store.runs()[0]
    assert row['total']==10 and row['completed']==10 and row['sites']=='[]'
    assert store.statistics(1)['progress_percent']==100
    Store(path)  # Migration is repeatable.


def test_export_explains_ok_but_unaccepted_and_has_raw_candidates(tmp_path):
    app=create_app(str(tmp_path/'db.sqlite3'))
    with TestClient(app) as client:
        rid,ids=seed(app.state.store)
        stats=client.get('/api/statistics',params={'run_id':rid}).json()
        assert stats['listings']['review']==2
        rows=list(csv.DictReader(io.StringIO(client.get('/api/export',params={'run_id':rid}).text.lstrip('\ufeff'))))
        row=rows[0]
        assert row['oson_status']=='ok' and row['oson_price']==''
        assert row['oson_candidates']=='1' and row['oson_review']=='1' and row['oson_accepted']=='0'
        assert 'review' in row['match_notes']
        candidate_rows=list(csv.DictReader(io.StringIO(client.get('/api/export/candidates',params={'run_id':rid}).text.lstrip('\ufeff'))))
        assert len(candidate_rows)==4
        oson=next(r for r in candidate_rows if r['site']=='oson')
        assert oson['price_uzs']=='90' and oson['match_status']=='review'
