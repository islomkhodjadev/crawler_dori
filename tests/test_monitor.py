import sqlite3
from datetime import datetime, timedelta, timezone

from dori.monitor import doctor, snapshot
from dori.storage import Store


def levels(rows):
    return {name: level for name, level, _ in rows}


def test_doctor_reports_missing_database_without_creating_it(tmp_path):
    path = tmp_path / 'absent.sqlite3'
    rows = doctor(str(path))
    assert levels(rows)['database'] == 'WARN'
    assert not path.exists()


def test_doctor_flags_silent_running_crawl_as_stalled(tmp_path):
    path = str(tmp_path / 'db.sqlite3')
    store = Store(path)
    store.import_products([{'name': 'Example 20 mg tab №30', 'manufacturer': 'Example'}], 'test')
    run = store.create_run(store.products(), sites=['arzon'])
    store.update_run(run, status='running')
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with sqlite3.connect(path) as db:
        db.execute('UPDATE runs SET last_activity=? WHERE id=?', (old, run))
    rows = doctor(path)
    assert levels(rows)[f'run {run}'] == 'FAIL'


def test_doctor_does_not_flag_fresh_or_waiting_runs(tmp_path):
    path = str(tmp_path / 'db.sqlite3')
    store = Store(path)
    store.import_products([{'name': 'Example 20 mg tab №30', 'manufacturer': 'Example'}], 'test')
    run = store.create_run(store.products(), sites=['arzon'])
    store.update_run(run, status='waiting_for_human')
    rows = doctor(path)
    assert f'run {run}' not in levels(rows)
    assert levels(rows)['verification'] == 'WARN'


def test_snapshot_describes_empty_and_active_runs(tmp_path):
    store = Store(str(tmp_path / 'db.sqlite3'))
    assert 'No crawl runs yet' in snapshot(store)
    ids = store.import_products([{'name': 'Example 20 mg tab №30', 'manufacturer': 'Example'}], 'test')
    run = store.create_run(store.products(ids), sites=['arzon'])
    store.update_run(run, status='running', completed=0, active_product_id=ids[0], active_site='arzon', phase='Searching')
    text = snapshot(store)
    assert f'Run {run}: running' in text
    assert 'products  0/1' in text
    assert 'active    Example 20 mg tab №30 @ arzon' in text
