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
 error TEXT, active_product_id INTEGER, active_site TEXT, phase TEXT,
 last_activity TEXT, sites TEXT NOT NULL DEFAULT '[]'
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
CREATE TABLE IF NOT EXISTS price_advice (
 run_id INTEGER NOT NULL, product_id INTEGER NOT NULL, price REAL NOT NULL, low REAL NOT NULL, high REAL NOT NULL,
 reason TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (run_id, product_id)
);
'''


class Store:
    def __init__(self, path=None):
        self.path = path or os.getenv('DORI_DB', 'data/dori.sqlite3')
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            existing={row['name'] for row in db.execute('PRAGMA table_info(runs)')}
            for name, declaration in {'active_product_id':'INTEGER','active_site':'TEXT','phase':'TEXT',
                                      'last_activity':'TEXT','sites':"TEXT NOT NULL DEFAULT '[]'"}.items():
                if name not in existing:
                    db.execute(f'ALTER TABLE runs ADD COLUMN {name} {declaration}')
            existing={row['name'] for row in db.execute('PRAGMA table_info(offers)')}
            for name, declaration in {'manual_price':'REAL','price_edited_at':'TEXT'}.items():
                if name not in existing:
                    db.execute(f'ALTER TABLE offers ADD COLUMN {name} {declaration}')

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

    def create_run(self, products, sites=None):
        with self.connect() as db:
            # Transaction prevents two requests from starting overlapping jobs.
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running','waiting_for_human')").fetchone():
                raise ValueError('A crawl is already queued or running')
            cursor = db.execute('INSERT INTO runs(started_at,status,total,last_activity,phase,sites) VALUES (?,?,?,?,?,?)',
                                (now(), 'queued', len(products), now(), 'Queued', json.dumps(list(sites or []))))
            return cursor.lastrowid

    def update_run(self, run_id, **changes):
        if not changes or not set(changes) <= {'status','completed','finished_at','error','active_product_id','active_site','phase','last_activity','sites'}:
            raise ValueError('Invalid run update')
        changes.setdefault('last_activity',now())
        with self.connect() as db:
            db.execute('UPDATE runs SET ' + ','.join(f'{key}=?' for key in changes) + ' WHERE id=?',
                       [*changes.values(), run_id])

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE runs SET status='interrupted',finished_at=?,active_product_id=NULL,active_site=NULL,phase='Interrupted by restart',error='Process restarted; start a new crawl' WHERE status IN ('queued','running','waiting_for_human')", (now(),))

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

    def save_advice(self, run_id, product_id, advice):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO price_advice VALUES (?,?,?,?,?,?,?,?)',
                       (run_id, product_id, advice['price'], advice['low'], advice['high'], advice['reason'], advice['model'], now()))

    def set_price(self, offer_id, price):
        """Store an operator-entered price that replaces the read one; None restores the read price."""
        if price is not None and not (0 < price < 1e9):
            raise ValueError('Price must be greater than zero')
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM offers WHERE id=?', (offer_id,)).fetchone():
                raise KeyError('Offer not found')
            db.execute('UPDATE offers SET manual_price=?,price_edited_at=? WHERE id=?',
                       (price, now() if price is not None else None, offer_id))

    def report(self, run_id=None):
        from .estimate import estimate
        with self.connect() as db:
            if run_id is None:
                row = db.execute('SELECT max(id) FROM runs').fetchone()
                run_id = row[0]
            rows = db.execute('SELECT * FROM results WHERE run_id=?', (run_id,)).fetchall()
            advice = {r['product_id']: dict(r) for r in db.execute('SELECT * FROM price_advice WHERE run_id=?', (run_id,))}
            grouped = {}
            for row in rows:
                product_id = row['product_id']
                entry = grouped.setdefault(product_id, {'product': self.products([product_id])[0], 'sources': {}, 'offers': []})
                entry['sources'][row['site']] = json.loads(row['details'])
                for offer in db.execute('SELECT * FROM offers WHERE result_id=?', (row['id'],)):
                    data = {**json.loads(offer['data']), 'id': offer['id'],
                            'match_status': offer['match_status'], 'score': offer['score'],
                            'reasons': json.loads(offer['reasons']), 'reviewed_at': offer['reviewed_at'],
                            'manual_price': offer['manual_price'], 'price_edited_at': offer['price_edited_at']}
                    if offer['manual_price'] is not None:
                        # The operator's price replaces the read one everywhere; the read price stays visible.
                        data['observed_price'], data['price'] = data['price'], offer['manual_price']
                    entry['offers'].append(data)
        from .matching import explain, match, summarize_matches
        result = []
        for entry in grouped.values():
            for offer in entry['offers']:
                # Explain with the current rules so older runs get accurate comments; the stored decision stays.
                current = match(entry['product'], offer)
                offer['comment'] = explain(entry['product'], offer, offer['match_status'], current['reasons'], offer['score'],
                                           reviewed=bool(offer.get('reviewed_at')), current_status=current['status'])
                if offer.get('manual_price') is not None:
                    was, now_ = (f'{v:,.0f}'.replace(',', ' ') for v in (offer['observed_price'], offer['price']))
                    offer['comment'] += f' Цена исправлена вручную: было {was} сум, стало {now_} сум.'
            entry['estimate'] = estimate(entry['offers'], entry['product'].get('invoice_price'))
            entry['advice'] = advice.get(entry['product']['id'])
            entry['matching']=summarize_matches(entry['offers'],entry['sources'])
            result.append(entry)
        return {'run_id': run_id, 'products': result}

    def statistics(self, run_id=None):
        """Count product coverage separately from candidate-listing decisions."""
        from datetime import datetime, timezone
        with self.connect() as db:
            db.execute('BEGIN')  # One consistent snapshot while the crawler writes.
            imported=db.execute('SELECT count(*) FROM products').fetchone()[0]
            if run_id is None:
                run_id=db.execute('SELECT max(id) FROM runs').fetchone()[0]
            row=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
            run=dict(row) if row else None
            candidate_rows=db.execute('''SELECT o.match_status,r.product_id,r.site
                FROM offers o JOIN results r ON r.id=o.result_id WHERE r.run_id=?''',(run_id,)).fetchall()
            results=[dict(r) for r in db.execute('SELECT * FROM results WHERE run_id=? ORDER BY id',(run_id,))]
            active=None
            if run and run['active_product_id']:
                p=db.execute('SELECT data FROM products WHERE id=?',(run['active_product_id'],)).fetchone()
                if p:active={'id':run['active_product_id'],'name':json.loads(p['data'])['name'],'site':run['active_site']}
            names={r['id']:json.loads(r['data'])['name'] for r in db.execute('''SELECT p.id,p.data FROM products p
                WHERE p.id IN (SELECT product_id FROM results WHERE run_id=?)''',(run_id,))}
        totals={'accepted':0,'rejected':0,'review':0,'total':len(candidate_rows)}
        matched=set(); reviews=set(); accepted_sites={}; source_stats={}
        source_names=json.loads(run['sites'] or '[]') if run else []
        for site in dict.fromkeys(source_names+[r['site'] for r in results]):
            source_stats[site]={'site':site,'checks_completed':0,'successful':0,'no_results':0,'errors':0,
                                'candidates':0,'accepted':0,'rejected':0,'review':0,'matched_products':0}
        per_result={}
        for r in results:
            source=source_stats[r['site']]
            source['checks_completed']+=1
            source['successful']+=r['status']=='ok'
            source['no_results']+=r['status']=='no_results'
            source['errors']+=r['status'] not in ('ok','no_results')
            per_result[r['product_id'],r['site']]={'accepted':0,'rejected':0,'review':0}
        for candidate in candidate_rows:
            status=candidate['match_status']
            if status in totals:totals[status]+=1
            source=source_stats[candidate['site']]
            source['candidates']+=1
            if status in ('accepted','rejected','review'):
                source[status]+=1
                per_result[candidate['product_id'],candidate['site']][status]+=1
            if status=='accepted':
                matched.add(candidate['product_id'])
                accepted_sites.setdefault(candidate['product_id'],set()).add(candidate['site'])
            if status=='review':reviews.add(candidate['product_id'])
        for site,source in source_stats.items():
            source['matched_products']=sum(site in sites for sites in accepted_sites.values())
        completed=run['completed'] if run else 0
        selected=run['total'] if run else 0
        checked_order=list(dict.fromkeys(r['product_id'] for r in results))
        completed_ids=set(checked_order[:completed])
        events=[]
        for r in results[-20:][::-1]:
            details=json.loads(r['details'])
            events.append({'product_id':r['product_id'],'name':names[r['product_id']],'site':r['site'],
                           'status':r['status'],'counts':per_result[r['product_id'],r['site']],
                           'message':details.get('error') or details.get('warning') or details.get('scope',''),
                           'extraction_method':details.get('extraction_method','page_parser')})
        elapsed=0
        if run:
            try:
                end=datetime.fromisoformat(run['finished_at']) if run['finished_at'] else datetime.now(timezone.utc)
                elapsed=max(0,int((end-datetime.fromisoformat(run['started_at'])).total_seconds()))
            except (ValueError,TypeError):pass
        return {'run_id':run_id,'run':run,'workspace_products':imported,
                'products':{'selected':selected,'completed':completed,'remaining':max(0,selected-completed),
                            'matched':len(matched),'review_only':len(reviews-matched),
                            'completed_without_match':len(completed_ids-matched),
                            'two_source_matches':sum({'oson','arzon'}<=s for s in accepted_sites.values())},
                'listings':totals,'sources':list(source_stats.values()),'events':events,
                'progress_percent':round(completed/selected*100,1) if selected else 0,
                'matched_percent':round(len(matched)/selected*100,1) if selected else 0,
                'elapsed_seconds':elapsed,'active':active}
