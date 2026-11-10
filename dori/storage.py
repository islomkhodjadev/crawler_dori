import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .adapters import now

SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY, batch TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 status TEXT NOT NULL, total INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
 error TEXT
);
CREATE TABLE IF NOT EXISTS results (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
 product_id INTEGER NOT NULL REFERENCES products(id), site TEXT NOT NULL,
 status TEXT NOT NULL, details TEXT NOT NULL,
 UNIQUE(run_id, product_id, site)
);
CREATE TABLE IF NOT EXISTS offers (
 id INTEGER PRIMARY KEY, result_id INTEGER NOT NULL REFERENCES results(id),
 data TEXT NOT NULL, match_status TEXT NOT NULL, score REAL NOT NULL,
 reasons TEXT NOT NULL, reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS offers_result ON offers(result_id);
'''


class Store:
    def __init__(self, path=None):
        self.path = path or os.getenv('DORI_DB', 'data/dori.sqlite3')
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def import_products(self, products, batch):
        with self.connect() as db:
            ids = []
            for product in products:
                cursor = db.execute('INSERT INTO products(batch,data) VALUES (?,?)',
                                    (batch, json.dumps(product, ensure_ascii=False)))
                ids.append(cursor.lastrowid)
        return ids

    def products(self, ids=None):
        with self.connect() as db:
            if ids is None:
                rows = db.execute('SELECT * FROM products ORDER BY id').fetchall()
            elif not ids:
                return []
            else:
                rows = db.execute('SELECT * FROM products WHERE id IN (' + ','.join('?' for _ in ids) + ') ORDER BY id', ids).fetchall()
        return [dict(id=r['id'], batch=r['batch'], **json.loads(r['data'])) for r in rows]

    def create_run(self, products):
        with self.connect() as db:
            # Transaction prevents two requests from starting overlapping jobs.
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running')").fetchone():
                raise ValueError('A crawl is already queued or running')
            cursor = db.execute('INSERT INTO runs(started_at,status,total) VALUES (?,?,?)',
                                (now(), 'queued', len(products)))
            return cursor.lastrowid

    def update_run(self, run_id, **changes):
        if not changes or not set(changes) <= {'status','completed','finished_at','error'}:
            raise ValueError('Invalid run update')
        with self.connect() as db:
            db.execute('UPDATE runs SET ' + ','.join(f'{key}=?' for key in changes) + ' WHERE id=?',
                       [*changes.values(), run_id])

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE runs SET status='interrupted',finished_at=?,error='Process restarted; start a new crawl' WHERE status IN ('queued','running')", (now(),))

    def runs(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 100')]

    def save_result(self, run_id, product_id, site, details, offers):
        from .matching import match
        product = self.products([product_id])[0]
        with self.connect() as db:
            cursor = db.execute('INSERT INTO results(run_id,product_id,site,status,details) VALUES (?,?,?,?,?)',
                                (run_id, product_id, site, details['status'], json.dumps(details)))
            for offer in offers:
                decision = match(product, offer)
                db.execute('INSERT INTO offers(result_id,data,match_status,score,reasons) VALUES (?,?,?,?,?)',
                           (cursor.lastrowid, json.dumps(offer, ensure_ascii=False), decision['status'],
                            decision['score'], json.dumps(decision['reasons'])))

    def review(self, offer_id, status):
        if status not in ('accepted','rejected'):
            raise ValueError('Choose accepted or rejected')
        with self.connect() as db:
            row = db.execute('SELECT reasons FROM offers WHERE id=?', (offer_id,)).fetchone()
            if not row:
                raise KeyError('Offer not found')
            reasons = json.loads(row['reasons'])
            # Hard attribute conflicts cannot be overridden as exact products.
            if status == 'accepted' and any('differs' in reason for reason in reasons):
                raise ValueError('Conflicting product attributes: correct the input and crawl again')
            db.execute('UPDATE offers SET match_status=?,reviewed_at=? WHERE id=?', (status, now(), offer_id))

    def report(self, run_id=None):
        from .estimate import estimate
        with self.connect() as db:
            if run_id is None:
                row = db.execute('SELECT max(id) FROM runs').fetchone()
                run_id = row[0]
            rows = db.execute('SELECT * FROM results WHERE run_id=?', (run_id,)).fetchall()
            grouped = {}
            for row in rows:
                product_id = row['product_id']
                entry = grouped.setdefault(product_id, {'product': self.products([product_id])[0], 'sources': {}, 'offers': []})
                entry['sources'][row['site']] = json.loads(row['details'])
                for offer in db.execute('SELECT * FROM offers WHERE result_id=?', (row['id'],)):
                    entry['offers'].append({**json.loads(offer['data']), 'id': offer['id'],
                                               'match_status': offer['match_status'], 'score': offer['score'],
                                               'reasons': json.loads(offer['reasons']), 'reviewed_at': offer['reviewed_at']})
        result = []
        for entry in grouped.values():
            entry['estimate'] = estimate(entry['offers'], entry['product'].get('invoice_price'))
            result.append(entry)
        return {'run_id': run_id, 'products': result}
