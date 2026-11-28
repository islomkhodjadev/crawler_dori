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
CREATE TABLE IF NOT EXISTS admin_users (
 id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
 password_hash TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
 token TEXT PRIMARY KEY, user_id TEXT NOT NULL,
 username TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS crawl_sessions (
 id TEXT PRIMARY KEY, run_id INTEGER NOT NULL, started_by TEXT NOT NULL,
 started_at REAL NOT NULL, finished_at REAL,
 total INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
 failed INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS crawl_queue (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
 product_id INTEGER NOT NULL, site TEXT NOT NULL DEFAULT 'oson',
 status TEXT NOT NULL DEFAULT 'pending',
 agent_id TEXT, started_at REAL, finished_at REAL,
 retries INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS agent_assignments (
 agent_id TEXT PRIMARY KEY, store_id INTEGER NOT NULL, lat REAL, lng REAL
);
CREATE TABLE IF NOT EXISTS competitor_locations (
 site TEXT NOT NULL, address TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL,
 PRIMARY KEY (site, address)
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
CREATE TABLE IF NOT EXISTS pharmacy_stock (
 id INTEGER PRIMARY KEY,
 store_id INTEGER NOT NULL,
 store_name TEXT NOT NULL,
 store_address TEXT NOT NULL DEFAULT '',
 lat REAL, lng REAL,
 dorixona_product_id INTEGER NOT NULL,
 dori_product_id INTEGER REFERENCES products(id),
 name TEXT NOT NULL,
 qty REAL NOT NULL,
 price REAL,
 synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pharmacy_stock_product ON pharmacy_stock(dori_product_id);
CREATE INDEX IF NOT EXISTS pharmacy_stock_dorixona ON pharmacy_stock(dorixona_product_id);
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
            if 'branch' not in existing:
                db.execute('ALTER TABLE runs ADD COLUMN branch TEXT')
            existing={row['name'] for row in db.execute('PRAGMA table_info(offers)')}
            for name, declaration in {'manual_price':'REAL','price_edited_at':'TEXT'}.items():
                if name not in existing:
                    db.execute(f'ALTER TABLE offers ADD COLUMN {name} {declaration}')
            # Migrate products table: add dorixona_product_id column if missing
            existing={row['name'] for row in db.execute('PRAGMA table_info(products)')}
            if 'dorixona_product_id' not in existing:
                db.execute('ALTER TABLE products ADD COLUMN dorixona_product_id INTEGER')
            # Migrate pharmacy_stock: add price column if missing
            existing={row['name'] for row in db.execute('PRAGMA table_info(pharmacy_stock)')}
            if 'price' not in existing:
                db.execute('ALTER TABLE pharmacy_stock ADD COLUMN price REAL')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.create_function("casefold", 1, lambda value: str(value).casefold() if value is not None else None, deterministic=True)
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

    def import_from_dorixona(self, products: list[dict], pharmacy_stock: list[dict], synced_at: str) -> list[int]:
        """Import unique products and their per-pharmacy stock from the internal API.

        Existing products with the same dorixona_product_id are reused (not duplicated).
        pharmacy_stock rows are replaced wholesale on each sync.
        Returns list of dori product ids (new + reused).
        """
        with self.connect() as db:
            # Build map: dorixona_product_id -> dori product id from previous syncs
            existing_map = {
                row['dorixona_product_id']: row['id']
                for row in db.execute(
                    "SELECT id, CAST(json_extract(data,'$.dorixona_product_id') AS INTEGER) AS dorixona_product_id "
                    "FROM products WHERE dorixona_product_id IS NOT NULL"
                )
                if row['dorixona_product_id']
            }
            ids = []
            for product in products:
                dpid = product.get('dorixona_product_id')
                if dpid and dpid in existing_map:
                    # Reuse existing product row but update its data
                    dori_id = existing_map[dpid]
                    db.execute('UPDATE products SET data=?, dorixona_product_id=? WHERE id=?',
                               (json.dumps(product, ensure_ascii=False), dpid, dori_id))
                    ids.append(dori_id)
                else:
                    cursor = db.execute(
                        'INSERT INTO products(batch, data, dorixona_product_id) VALUES (?,?,?)',
                        ('03Dorixona', json.dumps(product, ensure_ascii=False), dpid)
                    )
                    dori_id = cursor.lastrowid
                    if dpid:
                        existing_map[dpid] = dori_id
                    ids.append(dori_id)

            # Rebuild dpid -> dori_id map including newly inserted rows
            dpid_to_dori = {p.get('dorixona_product_id'): dori_id
                            for p, dori_id in zip(products, ids)
                            if p.get('dorixona_product_id')}

            # Replace pharmacy stock: delete all existing rows, re-insert fresh
            db.execute('DELETE FROM pharmacy_stock')
            for row in pharmacy_stock:
                dpid = row['dorixona_product_id']
                dori_id = dpid_to_dori.get(dpid) or existing_map.get(dpid)
                db.execute(
                    'INSERT INTO pharmacy_stock(store_id,store_name,store_address,lat,lng,'
                    'dorixona_product_id,dori_product_id,name,qty,price,synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (row['store_id'], row['store_name'], row['store_address'],
                     row.get('lat'), row.get('lng'), dpid, dori_id,
                     row['name'], row['qty'], row.get('price'), synced_at)
                )
        return ids

    def pharmacy_stock_for_products(self, dori_product_ids: list[int]) -> dict[int, list[dict]]:
        """Return pharmacy stock rows grouped by dori_product_id."""
        if not dori_product_ids:
            return {}
        placeholders = ','.join('?' * len(dori_product_ids))
        with self.connect() as db:
            rows = db.execute(
                f'SELECT * FROM pharmacy_stock WHERE dori_product_id IN ({placeholders})',
                dori_product_ids
            ).fetchall()
        result: dict[int, list[dict]] = {}
        for row in rows:
            pid = row['dori_product_id']
            result.setdefault(pid, []).append(dict(row))
        return result

    def stores(self) -> list[dict]:
        """Return distinct stores from pharmacy_stock with product counts."""
        with self.connect() as db:
            rows = db.execute(
                'SELECT store_id, store_name, store_address, lat, lng, COUNT(*) as products '
                'FROM pharmacy_stock GROUP BY store_id ORDER BY store_name'
            ).fetchall()
        return [dict(r) for r in rows]

    def agent_assignments(self):
        with self.connect() as db:
            return {r['agent_id']: dict(r) for r in db.execute('SELECT * FROM agent_assignments')}

    def assign_agent(self, agent_id, store_id, lat, lng):
        with self.connect() as db:
            if store_id is None:
                db.execute('DELETE FROM agent_assignments WHERE agent_id=?', (agent_id,))
            else:
                db.execute('INSERT OR REPLACE INTO agent_assignments VALUES (?, ?, ?, ?)',
                           (agent_id, store_id, lat, lng))

    def competitor_locations(self):
        with self.connect() as db:
            return {(r['site'], r['address']): (r['lat'], r['lng']) for r in
                    db.execute('SELECT * FROM competitor_locations')}

    def set_competitor_location(self, offer_id, point):
        from .local_area import address_key, mark_local_offers

        with self.connect() as db:
            row = db.execute(
                'SELECT o.data, r.run_id, r.product_id FROM offers o JOIN results r ON r.id=o.result_id WHERE o.id=?',
                (offer_id,),
            ).fetchone()
            if not row:
                raise KeyError('Offer not found')
            offer = json.loads(row['data'])
            key = address_key(offer)
            if not key or 'location_status' not in offer or offer.get('price_kind') != 'per_pharmacy':
                raise ValueError('This offer has no pharmacy address for a local comparison')
            db.execute('INSERT OR REPLACE INTO competitor_locations VALUES (?, ?, ?, ?)', (*key, *point))
            offer['latitude'], offer['longitude'] = point
            mark_local_offers([offer], (offer['search_lat'], offer['search_lng']),
                              offer['radius_km'], offer['search_agent'])
            db.execute('UPDATE offers SET data=? WHERE id=?', (json.dumps(offer, ensure_ascii=False), offer_id))
            db.execute('DELETE FROM price_advice WHERE run_id=? AND product_id=?',
                       (row['run_id'], row['product_id']))

    def create_branch_runs(self, branches, sites):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running','waiting_for_human')").fetchone():
                raise ValueError('A crawl is already queued or running')
            return [
                db.execute(
                    'INSERT INTO runs(started_at,status,total,last_activity,phase,sites,branch) VALUES (?,?,?,?,?,?,?)',
                    (now(), 'queued', len(products), now(), 'Waiting for branch agents',
                     json.dumps(list(sites)), json.dumps({**branch, 'prices': {
                         p['id']: p.get('invoice_price') for p in products
                     }}, ensure_ascii=False)),
                ).lastrowid
                for branch, products in branches
            ]

    def products(self, ids=None, search='', page=0, page_size=50, store_ids: list[int] | None = None):
        with self.connect() as db:
            if ids is not None:
                if not ids:
                    return []
                rows = db.execute('SELECT * FROM products WHERE id IN (' + ','.join('?' for _ in ids) + ') ORDER BY id', ids).fetchall()
                return [dict(id=r['id'], batch=r['batch'], **json.loads(r['data'])) for r in rows]
            # Paginated + search + optional store filter
            like = f'%{search.casefold()}%' if search else '%'
            if store_ids:
                sph = ','.join('?' * len(store_ids))
                base_where = (
                    f"products.id IN (SELECT dori_product_id FROM pharmacy_stock WHERE store_id IN ({sph})) "
                    f"AND (casefold(json_extract(products.data,'$.name')) LIKE ? OR casefold(json_extract(products.data,'$.manufacturer')) LIKE ?)"
                )
                params_count = [*store_ids, like, like]
                params_rows  = [*store_ids, like, like, page_size, page * page_size]
                count = db.execute(f"SELECT COUNT(*) FROM products WHERE {base_where}", params_count).fetchone()[0]
                rows  = db.execute(f"SELECT * FROM products WHERE {base_where} ORDER BY id LIMIT ? OFFSET ?", params_rows).fetchall()
            else:
                base_where = "casefold(json_extract(data,'$.name')) LIKE ? OR casefold(json_extract(data,'$.manufacturer')) LIKE ?"
                count = db.execute(f"SELECT COUNT(*) FROM products WHERE {base_where}", (like, like)).fetchone()[0]
                rows  = db.execute(f"SELECT * FROM products WHERE {base_where} ORDER BY id LIMIT ? OFFSET ?",
                                   (like, like, page_size, page * page_size)).fetchall()
        items = [dict(id=r['id'], batch=r['batch'], **json.loads(r['data'])) for r in rows]
        return {'items': items, 'total': count, 'page': page, 'page_size': page_size,
                'pages': max(1, (count + page_size - 1) // page_size)}

    def products_all_ids(self, search='', store_ids: list[int] | None = None) -> list[int]:
        """Return all product ids matching search and optional store filter (for select-all)."""
        with self.connect() as db:
            like = f'%{search.casefold()}%' if search else '%'
            if store_ids:
                sph = ','.join('?' * len(store_ids))
                rows = db.execute(
                    f"SELECT id FROM products WHERE id IN (SELECT dori_product_id FROM pharmacy_stock WHERE store_id IN ({sph})) "
                    f"AND (casefold(json_extract(data,'$.name')) LIKE ? OR casefold(json_extract(data,'$.manufacturer')) LIKE ?) ORDER BY id",
                    [*store_ids, like, like]
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id FROM products WHERE casefold(json_extract(data,'$.name')) LIKE ? OR casefold(json_extract(data,'$.manufacturer')) LIKE ? ORDER BY id",
                    (like, like)
                ).fetchall()
        return [r['id'] for r in rows]

    def delete_products(self, batch: str | None = None, ids: list[int] | None = None):
        """Delete products (and their offers/results) by batch name or explicit id list.
        Returns count deleted."""
        with self.connect() as db:
            if ids is not None:
                if not ids:
                    return 0
                ph = ','.join('?' * len(ids))
                # Cascade: offers → results → products
                result_ids = [r[0] for r in db.execute(f'SELECT id FROM results WHERE product_id IN ({ph})', ids).fetchall()]
                if result_ids:
                    rph = ','.join('?' * len(result_ids))
                    db.execute(f'DELETE FROM offers WHERE result_id IN ({rph})', result_ids)
                    db.execute(f'DELETE FROM results WHERE id IN ({rph})', result_ids)
                db.execute(f'DELETE FROM pharmacy_stock WHERE dori_product_id IN ({ph})', ids)
                cur = db.execute(f'DELETE FROM products WHERE id IN ({ph})', ids)
                return cur.rowcount
            elif batch is not None:
                prod_ids = [r[0] for r in db.execute('SELECT id FROM products WHERE batch=?', (batch,)).fetchall()]
                if prod_ids:
                    return self.delete_products(ids=prod_ids)
                return 0
            return 0

    def create_run(self, products, sites=None, force=False):
        total = products if isinstance(products, int) else len(products)
        with self.connect() as db:
            if not force:
                # Transaction prevents two requests from starting overlapping jobs.
                db.execute('BEGIN IMMEDIATE')
                if db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running','waiting_for_human')").fetchone():
                    raise ValueError('A crawl is already queued or running')
            cursor = db.execute('INSERT INTO runs(started_at,status,total,last_activity,phase,sites) VALUES (?,?,?,?,?,?)',
                                (now(), 'queued', total, now(), 'Queued', json.dumps(list(sites or []))))
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
            runs = [dict(row) for row in db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 100')]
        for run in runs:
            if run['branch']:
                branch = json.loads(run['branch'])
                run['branch'] = {key: value for key, value in branch.items() if key != 'prices'}
        return runs

    def save_result(self, run_id, product_id, site, details, offers):
        from .matching import exact_match

        product = self.products([product_id])[0]
        verified = [(offer, exact_match(product, offer)) for offer in offers]
        verified = [(offer, decision) for offer, decision in verified if decision['status'] == 'accepted']
        if details['status'] == 'ok' and not verified:
            details = {**details, 'status': 'no_results', 'warning': 'Точное совпадение не найдено'}
        with self.connect() as db:
            cursor = db.execute('INSERT INTO results(run_id,product_id,site,status,details) VALUES (?,?,?,?,?)',
                                (run_id, product_id, site, details['status'], json.dumps(details)))
            for offer, decision in verified:
                db.execute('INSERT INTO offers(result_id,data,match_status,score,reasons) VALUES (?,?,?,?,?)',
                           (cursor.lastrowid, json.dumps(offer, ensure_ascii=False), decision['status'],
                            decision['score'], json.dumps(decision['reasons'])))

    def review(self, offer_id, status):
        if status not in ('accepted','rejected'):
            raise ValueError('Choose accepted or rejected')
        with self.connect() as db:
            row = db.execute(
                'SELECT o.data AS offer, p.data AS product, r.run_id, r.product_id '
                'FROM offers o JOIN results r ON r.id=o.result_id JOIN products p ON p.id=r.product_id WHERE o.id=?',
                (offer_id,),
            ).fetchone()
            if not row:
                raise KeyError('Offer not found')
            if status == 'accepted':
                from .matching import exact_match
                if exact_match(json.loads(row['product']), json.loads(row['offer']))['status'] != 'accepted':
                    raise ValueError('Подтвердить можно только точное совпадение товара и упаковки.')
            db.execute('DELETE FROM price_advice WHERE run_id=? AND product_id=?',
                       (row['run_id'], row['product_id']))
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
            context = db.execute('SELECT branch FROM runs WHERE id=?', (run_id,)).fetchone()
            branch = json.loads(context[0]) if context and context[0] else None
            rows = db.execute('SELECT * FROM results WHERE run_id=?', (run_id,)).fetchall()
            advice = {r['product_id']: dict(r) for r in db.execute('SELECT * FROM price_advice WHERE run_id=?', (run_id,))}
            products = {
                r['id']: dict(id=r['id'], batch=r['batch'], **json.loads(r['data']))
                for r in db.execute('SELECT * FROM products WHERE id IN (SELECT product_id FROM results WHERE run_id=?)', (run_id,))
            }
            offers_by_result = {}
            for offer in db.execute(
                'SELECT o.* FROM offers o JOIN results r ON r.id=o.result_id WHERE r.run_id=? ORDER BY o.id',
                (run_id,),
            ):
                offers_by_result.setdefault(offer['result_id'], []).append(offer)
            grouped = {}
            for row in rows:
                product_id = row['product_id']
                product = products[product_id]
                if branch:
                    product['invoice_price'] = branch['prices'].get(str(product_id))
                    product['branch_name'] = branch['store_name']
                entry = grouped.setdefault(product_id, {'product': product, 'sources': {}, 'offers': []})
                entry['sources'][row['site']] = json.loads(row['details'])
                for offer in offers_by_result.get(row['id'], ()):
                    data = {**json.loads(offer['data']), 'id': offer['id'],
                            'match_status': offer['match_status'], 'score': offer['score'],
                            'reasons': json.loads(offer['reasons']), 'reviewed_at': offer['reviewed_at'],
                            'manual_price': offer['manual_price'], 'price_edited_at': offer['price_edited_at']}
                    if offer['manual_price'] is not None:
                        # The operator's price replaces the read one everywhere; the read price stays visible.
                        data['observed_price'], data['price'] = data['price'], offer['manual_price']
                    entry['offers'].append(data)
        from .matching import exact_match, explain, summarize_matches
        result = []
        for entry in grouped.values():
            original = entry['offers']
            entry['offers'] = []
            for offer in original:
                current = exact_match(entry['product'], offer)
                if current['status'] != 'accepted':
                    continue
                entry['offers'].append(offer)
                offer['comment'] = explain(entry['product'], offer, offer['match_status'], current['reasons'], offer['score'],
                                           reviewed=bool(offer.get('reviewed_at')), current_status=current['status'])
                if offer.get('manual_price') is not None:
                    was, now_ = (f'{v:,.0f}'.replace(',', ' ') for v in (offer['observed_price'], offer['price']))
                    offer['comment'] += f' Цена исправлена вручную: было {was} сум, стало {now_} сум.'
            entry['estimate'] = estimate(entry['offers'], entry['product'].get('invoice_price'))
            entry['advice'] = (advice.get(entry['product']['id'])
                               if len(original) == len(entry['offers']) and entry['estimate']['value'] is not None else None)
            entry['matching']=summarize_matches(entry['offers'],entry['sources'])
            result.append(entry)
        if branch:
            branch.pop('prices')
        return {'run_id': run_id, 'products': result, 'branch': branch}

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
            if run and run['branch']:
                branch = json.loads(run['branch'])
                branch.pop('prices')
                run['branch'] = branch
            candidate_rows=db.execute('''SELECT o.match_status,r.product_id,r.site,o.data AS offer,p.data AS product
                FROM offers o JOIN results r ON r.id=o.result_id JOIN products p ON p.id=r.product_id WHERE r.run_id=?''',(run_id,)).fetchall()
            results=[dict(r) for r in db.execute('SELECT * FROM results WHERE run_id=? ORDER BY id',(run_id,))]
            active=None
            if run and run['active_product_id']:
                p=db.execute('SELECT data FROM products WHERE id=?',(run['active_product_id'],)).fetchone()
                if p:active={'id':run['active_product_id'],'name':json.loads(p['data'])['name'],'site':run['active_site']}
            names={r['id']:json.loads(r['data'])['name'] for r in db.execute('''SELECT p.id,p.data FROM products p
                WHERE p.id IN (SELECT product_id FROM results WHERE run_id=?)''',(run_id,))}
        from .matching import exact_match
        totals={'accepted':0,'rejected':0,'review':0,'total':0}
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
            if exact_match(json.loads(candidate['product']), json.loads(candidate['offer']))['status'] != 'accepted':
                continue
            totals['total'] += 1
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
