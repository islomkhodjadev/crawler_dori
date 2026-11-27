import asyncio
import os
import csv
import io
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .importer import read_products
from .service import crawl
from .local_area import coordinates, eligible_for_comparison
from .storage import Store
from .browser_admin import BrowserAdmin, SITES
from .auth import AdminAuth
from .agent import AgentBus
from .queue import CrawlQueue
from .logs import get, setup as setup_logging

log = get('http')


class CrawlRequest(BaseModel):
    product_ids: list[int] = Field(min_length=1, max_length=5000)
    sites: list[str] = Field(default_factory=lambda: ['arzon', 'oson'], min_length=1, max_length=2)
    use_ai: bool | None = None
    store_ids: list[int] = Field(default_factory=list, max_length=100)
    radius_km: float = Field(default=3, ge=0.1, le=25, allow_inf_nan=False)


class ConnectionRequest(BaseModel):
    product_id: int
    use_ai: bool | None = None


class ReviewRequest(BaseModel):
    status: str


class PriceRequest(BaseModel):
    price: float | None = Field(default=None, gt=0, lt=1e9, allow_inf_nan=False)


class ProductRequest(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    manufacturer: str = Field(default='', max_length=300)
    barcode: str = Field(default='', max_length=30)
    external_id: str = Field(default='', max_length=100)
    arzon_id: str = Field(default='', max_length=100)
    oson_id: str = Field(default='', max_length=100)
    invoice_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)


def fit_price(price, low, high):
    """Keep AI advice inside [low, high], rounded to 100 UZS when the range allows it."""
    import math
    price = min(max(price, low), high)
    rounded = round(price / 100) * 100
    if low <= rounded <= high:
        return rounded
    inner = [v for v in (math.ceil(low / 100) * 100, math.floor(high / 100) * 100) if low <= v <= high]
    return min(inner, key=lambda v: abs(v - price)) if inner else round(price)


def create_app(db_path=None):
    setup_logging()
    store = Store(db_path)
    tasks = set()
    run_tasks = {}
    browsers = BrowserAdmin()
    auth = AdminAuth(store.path)
    agent_bus = AgentBus()
    crawl_queue = CrawlQueue(store.path)

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        watchdog_task = asyncio.create_task(crawl_queue.watchdog())
        tasks.add(watchdog_task)
        yield
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await browsers.close()

    app = FastAPI(title='Dori · Pharmacy price comparison', lifespan=lifespan)
    app.state.store = store
    app.state.browsers = browsers
    auth.install(app)

    @app.middleware('http')
    async def access_log(request: Request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception('unhandled error %s %s', request.method, request.url.path)
            raise
        elapsed = (time.perf_counter() - started) * 1000
        # Request bodies are never logged: they can contain product data or passwords.
        level = log.warning if response.status_code >= 400 else log.info
        level('%s %s -> %s in %.0f ms', request.method, request.url.path, response.status_code, elapsed)
        return response

    @app.middleware('http')
    async def local_origin(request: Request, call_next):
        # Local tool: reject browser mutations originating at a different website.
        origin = request.headers.get('origin')
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin:
            from urllib.parse import urlparse
            if urlparse(origin).netloc != request.headers.get('host'):
                return Response('Cross-origin requests are not allowed', status_code=403)
        return await call_next(request)

    static = Path(__file__).parent / 'static'
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.get('/')
    def index():
        return FileResponse(static / 'index.html')

    @app.get('/api/health')
    def health():
        import os
        return {'ok': True, 'ai_configured': bool(os.getenv('OPENAI_API_KEY')),
                'ai_default_enabled': bool(os.getenv('OPENAI_API_KEY')),
                'admin_password_set': True,
                'agent_connected': agent_bus.connected() > 0,
                'agent_count': agent_bus.connected(),
                'queue_running': crawl_queue.is_running}

    @app.post('/api/import')
    async def upload(file: UploadFile = File(...)):
        data = await file.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(413, 'Maximum file size is 10 MB')
        try:
            products = await asyncio.to_thread(read_products, data, file.filename or '')
            if len(products) > 5000:
                raise ValueError('Maximum 5000 product rows per upload')
            ids = store.import_products(products, Path(file.filename or 'upload').name)
        except Exception as exc:
            raise HTTPException(400, 'Import failed: ' + str(exc)[:300]) from exc
        return {'count': len(ids), 'product_ids': ids}

    @app.get('/api/products')
    def products(search: str = '', page: int = 0, page_size: int = 50, store_ids: str = ''):
        page_size = max(10, min(200, page_size))
        sid_list = [int(x) for x in store_ids.split(',') if x.strip().isdigit()] if store_ids else None
        return store.products(search=search, page=page, page_size=page_size, store_ids=sid_list)

    @app.get('/api/products/ids')
    def product_ids(search: str = '', store_ids: str = ''):
        """All product ids matching search — used for select-all."""
        sid_list = [int(x) for x in store_ids.split(',') if x.strip().isdigit()] if store_ids else None
        return {'ids': store.products_all_ids(search=search, store_ids=sid_list)}

    class DeleteProductsRequest(BaseModel):
        batch: str | None = None
        ids: list[int] | None = None

    @app.delete('/api/products')
    def delete_products(body: DeleteProductsRequest):
        if body.batch is None and body.ids is None:
            raise HTTPException(400, 'Provide batch or ids')
        count = store.delete_products(batch=body.batch, ids=body.ids)
        return {'deleted': count}

    @app.post('/api/products')
    def add_product(body: ProductRequest):
        data = body.model_dump()
        data.update(quantity=None, source_row=None, sheet='Manual')
        ids = store.import_products([data], 'Manual')
        return store.products(ids)[0]

    @app.get('/api/runs')
    def runs():
        return store.runs()

    def start_collection(run_id, products, sites, use_ai, branch=None):
        task = asyncio.create_task(crawl(store, run_id, products, sites, admin=browsers,
                                         use_ai=use_ai, agent_bus=agent_bus, branch=branch))
        run_tasks[run_id] = task
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(lambda _: run_tasks.pop(run_id, None))

    @app.post('/api/crawl')
    async def start(body: CrawlRequest):
        if not body.sites or not set(body.sites) <= {'arzon', 'oson'}:
            raise HTTPException(400, 'Supported sites: arzon, oson')
        use_ai = body.use_ai if body.use_ai is not None else bool(os.getenv('OPENAI_API_KEY'))
        if use_ai and not os.getenv('OPENAI_API_KEY'):
            raise HTTPException(400, 'Configure OPENAI_API_KEY before enabling AI parsing')
        products = store.products(list(set(body.product_ids)))
        if len(products) != len(set(body.product_ids)):
            raise HTTPException(404, 'Some products do not exist')
        sites = tuple(dict.fromkeys(body.sites))
        if body.store_ids:
            available = {branch['store_id']: branch for branch in store.stores()}
            stock = store.pharmacy_stock_for_products([product['id'] for product in products])
            stock_by_store = {}
            for product_id, rows in stock.items():
                for row in rows:
                    by_product = stock_by_store.setdefault(row['store_id'], {})
                    previous = by_product.get(product_id)
                    if previous is None or (row['price'] and row['price'] > 0):
                        by_product[product_id] = row['price']
            branches = []
            for store_id in dict.fromkeys(body.store_ids):
                if store_id not in available:
                    raise HTTPException(404, 'Pharmacy branch not found')
                branch = {**available[store_id], 'radius_km': body.radius_km}
                prices = stock_by_store.get(store_id, {})
                branch_products = [{**product, 'invoice_price': prices[product['id']]}
                                   for product in products if product['id'] in prices]
                if branch_products:
                    branches.append((branch, branch_products))
            if not branches:
                raise HTTPException(400, 'No selected products belong to these branches')
            try:
                run_ids = store.create_branch_runs(branches, sites)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            for run_id, (branch, branch_products) in zip(run_ids, branches):
                start_collection(run_id, branch_products, sites, use_ai, branch)
            return {'run_id': run_ids[0], 'run_ids': run_ids}

        agent_sites = {'oson', 'arzon'} if agent_bus.connected() else set()
        try:
            browsers.require_ready([site for site in sites if site not in agent_sites])
            run_id = store.create_run(products, sites=sites)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        start_collection(run_id, products, sites, use_ai)
        return {'run_id': run_id}

    @app.get('/api/browsers')
    async def browser_status():
        return browsers.state()

    @app.post('/api/browsers/{site}/open')
    async def open_browser(site: str):
        try:
            await browsers.open(site)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            hint = ('Install Google Chrome on the machine running this app, or set DORI_BROWSER_CHANNEL=chromium.'
                    if os.getenv('DORI_BROWSER_CHANNEL','chromium').lower()=='chrome'
                    else 'Run python -m playwright install chromium.')
            raise HTTPException(503, 'Browser could not start: '+type(exc).__name__+'. '+hint+' Visible mode also requires a desktop display.') from exc
        return browsers.state()[site]

    @app.post('/api/browsers/{site}/check')
    async def check_browser(site: str, body: ConnectionRequest):
        if site not in SITES:
            raise HTTPException(404, 'Unknown website')
        use_ai = body.use_ai if body.use_ai is not None else bool(os.getenv('OPENAI_API_KEY'))
        if use_ai and not os.getenv('OPENAI_API_KEY'):
            raise HTTPException(400, 'Configure OPENAI_API_KEY to enable AI parsing')
        products = store.products([body.product_id])
        if not products:
            raise HTTPException(404, 'Select an imported product for the connection test')
        try:
            return await browsers.check(site, products[0], use_ai=use_ai)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.websocket('/api/browsers/{site}/stream')
    async def browser_stream(websocket: WebSocket, site: str):
        from urllib.parse import urlparse
        origin = websocket.headers.get('origin', '')
        if (not origin or urlparse(origin).netloc != websocket.headers.get('host')
                or not auth.authorized(websocket.cookies) or site not in SITES):
            await websocket.close(code=1008)
            return
        await browsers.stream(websocket, site)

    @app.post('/api/runs/{run_id}/cancel')
    async def cancel(run_id: int):
        if crawl_queue.is_running and crawl_queue.status().get('run_id') == run_id:
            return await queue_stop()
        task = run_tasks.get(run_id)
        if not task:
            raise HTTPException(404, 'Active run not found')
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        from .adapters import now
        store.update_run(run_id, status='interrupted', finished_at=now(), active_product_id=None, active_site=None, phase='Cancelled', error='Collection cancelled by admin')
        return {'ok': True}

    # ------------------------------------------------------------------ agent --
    @app.get('/api/agent/status')
    def agent_status():
        """How many Chrome extensions are currently connected."""
        return {'connected': agent_bus.connected()}

    @app.get('/api/agents')
    def agents_list():
        """List all currently connected Chrome extension agents."""
        assignments = store.agent_assignments()
        return [{**agent, 'assignment': assignments.get(agent['id'])} for agent in agent_bus.agent_list()]

    class AgentAssignmentRequest(BaseModel):
        store_id: int | None = None
        lat: float | None = None
        lng: float | None = None

    @app.put('/api/agents/{agent_id}/assignment')
    def put_agent_assignment(agent_id: str, body: AgentAssignmentRequest):
        if not any(agent['id'] == agent_id for agent in agent_bus.agent_list()):
            raise HTTPException(404, 'Agent is offline')
        if body.store_id is not None:
            branch = next((item for item in store.stores() if item['store_id'] == body.store_id), None)
            if not branch:
                raise HTTPException(404, 'Pharmacy branch not found')
            if body.lat is not None or body.lng is not None:
                if not coordinates(body.lat, body.lng):
                    raise HTTPException(400, 'Enter valid latitude and longitude together')
            elif not coordinates(branch['lat'], branch['lng']):
                raise HTTPException(400, 'Branch coordinates are missing. Enter the agent location.')
        store.assign_agent(agent_id, body.store_id, body.lat, body.lng)
        return {'ok': True}

    # ------------------------------------------------------------------ admin user management --

    @app.get('/api/admin/users')
    def admin_list_users():
        return {'users': auth.list_users()}

    class CreateUserRequest(BaseModel):
        username: str
        password: str

    @app.post('/api/admin/users')
    def admin_create_user(body: CreateUserRequest):
        try:
            user = auth.create_user(body.username, body.password)
            return user
        except ValueError as e:
            raise HTTPException(400, str(e))

    class SetPasswordRequest(BaseModel):
        password: str

    @app.put('/api/admin/users/{user_id}/password')
    async def admin_set_password(user_id: str, body: SetPasswordRequest, request: Request):
        try:
            auth.set_password(user_id, body.password)
            return {'ok': True}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete('/api/admin/users/{user_id}')
    async def admin_delete_user(user_id: str, request: Request):
        requesting = auth.current_user(request.cookies) or ''
        try:
            auth.delete_user(user_id, requesting)
            return {'ok': True}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post('/api/agent/check')
    async def agent_check(body: ConnectionRequest):
        """Send a real Oson search through the connected agent and verify it returns prices."""
        if not agent_bus.connected():
            raise HTTPException(503, 'No Chrome extension connected. Install the extension and connect it first.')
        products = store.products([body.product_id])
        if not products:
            raise HTTPException(404, 'Select an imported product for the connection test')
        from .adapters import BrowserCrawler
        from .matching import search_term
        from urllib.parse import urlencode
        product = products[0]
        agent_url = 'https://osonapteka.uz/search?' + urlencode({'q': search_term(product)})
        result = await agent_bus.fetch(agent_url, 'oson')
        if not result or not result.get('html'):
            raise HTTPException(502, 'Extension timed out or returned no page. Check that your Chrome is open and the extension is connected.')
        from bs4 import BeautifulSoup
        from .adapters import parse_oson_jsonld
        from .oson_cards import parse_oson_cards
        from .page_state import classify_text
        html = result['html']
        final_url = result.get('url', agent_url)
        offers = parse_oson_jsonld(html, final_url)
        method = 'json_ld'
        if not offers:
            offers = parse_oson_cards(html, final_url)
            method = 'visible_cards'
        body_text = BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
        state = classify_text(body_text)
        if offers:
            return {'ok': True, 'listings': len(offers), 'method': method,
                    'preview': [{'name': o.get('name',''), 'price': o.get('price'), 'currency': 'UZS'} for o in offers[:3]]}
        if state == 'no_results':
            return {'ok': True, 'listings': 0, 'method': 'no_results', 'preview': []}
        return {'ok': False, 'state': state, 'error': f'Page loaded (state: {state}) but no priced offers parsed. Try a different test product.'}

    @app.get('/api/agent/extension.zip')
    def agent_extension_zip():
        """Serve the Chrome extension as a downloadable zip."""
        import io, zipfile
        # chrome-extension/ lives next to the project root (cwd when the service starts),
        # or can be overridden with DORI_EXTENSION_DIR. __file__ is unreliable when the
        # package is installed into a virtualenv (it resolves inside site-packages).
        ext_dir = Path(os.getenv('DORI_EXTENSION_DIR') or
                       Path.cwd() / 'chrome-extension')
        if not ext_dir.exists():
            raise HTTPException(404, f'Extension directory not found: {ext_dir}')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(ext_dir.rglob('*')):
                if f.is_file() and not f.name.startswith('.'):
                    zf.write(f, f.relative_to(ext_dir))
        buf.seek(0)
        return Response(buf.read(), media_type='application/zip',
                        headers={'Content-Disposition': 'attachment; filename="dori-agent.zip"'})

    @app.websocket('/api/agent/ws')
    async def agent_ws(websocket: WebSocket, password: str = '',
                       agent_id: str = '', agent_name: str = ''):
        """Chrome extension connects here. Auth via ?password= query param."""
        from urllib.parse import urlparse
        origin = websocket.headers.get('origin', '')
        host = websocket.headers.get('host', '')
        # Allow connection from the extension (chrome-extension:// origin) or same host.
        origin_ok = (not origin
                     or urlparse(origin).scheme == 'chrome-extension'
                     or urlparse(origin).netloc == host)
        if not origin_ok:
            await websocket.close(code=1008, reason='Cross-origin')
            return
        if not auth.check_ws_password(password):
            await websocket.close(code=1008, reason='Unauthorized')
            log.warning('agent ws rejected wrong password origin=%s', origin)
            return
        await websocket.accept()
        await agent_bus.attach(websocket, agent_id=agent_id, agent_name=agent_name)

    # ------------------------------------------------------------------ queue --

    class QueueStartRequest(BaseModel):
        sites: list[str] = Field(default_factory=lambda: ['arzon', 'oson'])
        product_ids: list[int] | None = None  # None = all products

    @app.post('/api/queue/start')
    async def queue_start(body: QueueStartRequest, request: Request):
        if crawl_queue.is_running:
            raise HTTPException(409, 'A queue crawl is already running')
        if not body.sites or not set(body.sites) <= {'arzon', 'oson'}:
            raise HTTPException(400, 'Supported sites: arzon, oson')
        if not agent_bus.connected():
            raise HTTPException(503, 'No Chrome extension agents connected')
        if body.product_ids is not None:
            if not body.product_ids:
                raise HTTPException(400, 'No product IDs provided')
            product_ids = list(set(body.product_ids))
        else:
            product_ids = store.products_all_ids()
        if not product_ids:
            raise HTTPException(404, 'No products found')
        username = auth.current_user(request.cookies) or 'admin'
        sites = tuple(dict.fromkeys(body.sites))
        total = len(product_ids) * len(sites)
        try:
            run_id = store.create_run(total, sites=sites)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        store.update_run(run_id, status='running', phase='Queue crawl starting')
        session_id = crawl_queue.create_session(run_id, username, total)
        crawl_queue.enqueue(session_id, product_ids, sites)
        agent_bus.start_queue_workers(crawl_queue, store, run_id)
        return {'session_id': session_id, 'run_id': run_id, 'total': total,
                'products': len(product_ids), 'sites': list(sites)}

    @app.post('/api/queue/stop')
    async def queue_stop():
        if not crawl_queue.is_running:
            raise HTTPException(404, 'No active queue session')
        status = crawl_queue.status()
        run_id = status.get('run_id')
        crawl_queue.stop_session()
        agent_bus.stop_queue_workers()
        if run_id:
            from .adapters import now
            store.update_run(run_id, status='interrupted', finished_at=now(),
                             active_product_id=None, active_site=None, phase='Queue stopped by admin')
        return {'ok': True}

    @app.get('/api/queue/status')
    def queue_status():
        return crawl_queue.status()

    # ------------------------------------------------------------------ /agent --

    @app.get('/api/statistics')
    def statistics(run_id: int | None = None):
        return store.statistics(run_id)

    class CompetitorLocationRequest(BaseModel):
        lat: float
        lng: float

    @app.put('/api/offers/{offer_id}/location')
    def set_offer_location(offer_id: int, body: CompetitorLocationRequest):
        point = coordinates(body.lat, body.lng)
        if not point:
            raise HTTPException(400, 'Enter valid latitude and longitude')
        try:
            store.set_competitor_location(offer_id, point)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {'ok': True}

    @app.get('/api/report')
    def report(run_id: int | None = None):
        return store.report(run_id)

    @app.post('/api/offers/{offer_id}/review')
    def review(offer_id: int, body: ReviewRequest):
        try:
            store.review(offer_id, body.status)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {'ok': True}

    @app.post('/api/offers/{offer_id}/price')
    def set_price(offer_id: int, body: PriceRequest):
        try:
            store.set_price(offer_id, body.price)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        log.info('offer %s price %s', offer_id, 'reset to read price' if body.price is None else f'set manually to {body.price}')
        return {'ok': True}

    @app.post('/api/products/{product_id}/optimal-price')
    async def optimal_price(product_id: int, run_id: int | None = None):
        from .ai import AIExtractionError, recommend_price
        report = store.report(run_id)
        entry = next((e for e in report['products'] if e['product']['id'] == product_id), None)
        if not entry:
            raise HTTPException(404, 'Product is not part of this collection')
        confirmed = [{'site': o['site'], 'name': o['name'], 'price': o['price']}
                     for o in entry['offers'] if eligible_for_comparison(o) and o['match_status'] == 'accepted' and o['price'] > 0]
        if not confirmed:
            raise HTTPException(409, 'Нет подтверждённых цен конкурентов — сначала подтвердите совпадения.')
        ours = entry['product'].get('invoice_price')
        prices = [o['price'] for o in confirmed] + ([ours] if ours else [])
        # Filter outliers: drop prices more than 2.5× below or above the median
        from statistics import median
        med = median(prices)
        prices_filtered = [p for p in prices if med / 3 <= p <= med * 3]
        if prices_filtered:
            prices = prices_filtered
        low, high = min(prices), max(prices)
        try:
            advice = await recommend_price(entry['product'], ours, confirmed, low, high)
        except AIExtractionError as exc:
            raise HTTPException(400, str(exc)) from exc
        price = fit_price(advice['price'], low, high)
        if price != advice['price']:
            log.warning('price advice for product %s adjusted from %s to %s (range %s-%s)', product_id, advice['price'], price, low, high)
        advice.update(price=price, low=low, high=high)
        store.save_advice(report['run_id'], product_id, advice)
        return advice

    @app.post('/api/products/{product_id}/suggest-attributes')
    async def ai_suggest(product_id: int):
        from .ai import suggest_attributes
        found = store.products([product_id])
        if not found:
            raise HTTPException(404, 'Product not found')
        try:
            suggestion = await suggest_attributes(found[0])
        except Exception as exc:
            # Do not include provider request objects or authentication headers in errors.
            message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            raise HTTPException(400, message[:300]) from exc
        return {'suggestion': suggestion, 'applied': False}

    @app.get('/api/export')
    def export(run_id: int | None = None):
        report = store.report(run_id)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['product_id','name','manufacturer','invoice_price','arzon_price','oson_price',
                         'estimate_uzs','estimate_status','invoice_delta_percent','arzon_status','oson_status','sources',
                         'match_status','arzon_candidates','arzon_accepted','arzon_review','arzon_rejected',
                         'oson_candidates','oson_accepted','oson_review','oson_rejected','match_notes',
                         'optimal_price_uzs','optimal_price_reason']
                        + (['branch_id', 'branch_name', 'radius_km'] if report['branch'] else []))
        for entry in report['products']:
            p, e = entry['product'], entry['estimate']
            row = [p['id'], p['name'], p['manufacturer'], p.get('invoice_price'), e['per_site'].get('arzon'),
                   e['per_site'].get('oson'), e['value'], e['status'], e['invoice_delta_percent'],
                   entry['sources'].get('arzon', {}).get('status'), entry['sources'].get('oson', {}).get('status'),
                   ' | '.join(o['url'] for o in entry['offers'] if eligible_for_comparison(o) and o['match_status'] == 'accepted')]
            summary=entry['matching']
            row.append(summary['status'])
            for site in ('arzon','oson'):
                row.extend(summary['per_site'].get(site,{}).get(key,0) for key in ('candidates','accepted','review','rejected'))
            row.append(summary['notes'])
            row.extend([entry['advice']['price'], entry['advice']['reason']] if entry.get('advice') else ['', ''])
            if report['branch']:
                branch = report['branch']
                row.extend([branch['store_id'], branch['store_name'], branch['radius_km']])
            # Prevent spreadsheet formula execution from untrusted names/manufacturers.
            writer.writerow(["'" + v if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')) else v for v in row])
        return Response('\ufeff' + out.getvalue(), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="dori-prices.csv"'})

    @app.get('/api/export/candidates')
    def export_candidates(run_id: int | None = None):
        out=io.StringIO()
        writer=csv.writer(out)
        writer.writerow(['product_id','input_name','site','listing_id','listing_name','manufacturer','price_uzs',
                         'match_status','name_score','reasons','observed_at','source_url','reviewed_at','comment',
                         'read_price_uzs','manual_price_uzs'])
        for entry in store.report(run_id)['products']:
            for offer in entry['offers']:
                row=[entry['product']['id'],entry['product']['name'],offer['site'],offer['id'],offer['name'],
                     offer.get('manufacturer',''),offer['price'],offer['match_status'],offer['score'],
                     ' | '.join(offer['reasons']),offer.get('observed_at',''),offer['url'],offer.get('reviewed_at',''),offer.get('comment',''),
                     offer.get('observed_price',offer['price']),offer.get('manual_price') or '']
                writer.writerow(["'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in row])
        return Response('\ufeff'+out.getvalue(),media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition':'attachment; filename="dori-candidates.csv"'})

    @app.get('/api/export/xlsx')
    def export_xlsx(run_id: int | None = None):
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
        report = store.report(run_id)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = '\u0426\u0435\u043d\u044b'
        header_font = Font(bold=True, color='FFFFFF')
        header_fill = PatternFill('solid', fgColor='1D6B55')
        headers = ['\u0422\u043e\u0432\u0430\u0440','\u041f\u0440\u043e\u0438\u0437\u0432\u043e\u0434\u0438\u0442\u0435\u043b\u044c','\u041d\u0430\u0448\u0430 \u0446\u0435\u043d\u0430','Arzon','Oson','\u0420\u044b\u043d\u043e\u0447\u043d\u0430\u044f \u0446\u0435\u043d\u0430','\u041e\u043f\u0442\u0438\u043c\u0430\u043b\u044c\u043d\u0430\u044f \u0446\u0435\u043d\u0430','\u041a \u043d\u0430\u0448\u0435\u0439 \u0446\u0435\u043d\u0435 %','\u0421\u0442\u0430\u0442\u0443\u0441']
        if report['branch']:
            headers.extend(['Филиал', 'Радиус, км'])
        for col, h in enumerate(headers, 1):
            cell = ws.cell(1, col, h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='center')
        ws.row_dimensions[1].height = 20
        for entry in report['products']:
            p, e = entry['product'], entry['estimate']
            delta = e.get('invoice_delta_percent')
            opt = entry['advice']['price'] if entry.get('advice') else None
            row = [
                p['name'], p.get('manufacturer',''),
                p.get('invoice_price'), e['per_site'].get('arzon'), e['per_site'].get('oson'),
                e.get('value'), opt,
                f"{delta:+.1f}%" if delta is not None else '',
                e.get('status',''),
            ]
            if report['branch']:
                row.extend([report['branch']['store_name'], report['branch']['radius_km']])
            ws.append(["'" + v if isinstance(v, str) and v.lstrip().startswith(('=','+','-','@')) else v for v in row])
        # Column widths
        for col, width in zip(range(1, 10), [45, 25, 14, 14, 14, 14, 16, 12, 14]):
            ws.column_dimensions[get_column_letter(col)].width = width
        if report['branch']:
            ws.column_dimensions['J'].width = 30
            ws.column_dimensions['K'].width = 14
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return Response(buf.read(), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': 'attachment; filename="dori-prices.xlsx"'})

    @app.get('/api/export/candidates/xlsx')
    def export_candidates_xlsx(run_id: int | None = None):
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = '\u0412\u0441\u0435 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u044f'
        header_font = Font(bold=True, color='FFFFFF')
        header_fill = PatternFill('solid', fgColor='1D6B55')
        headers = ['ID \u0442\u043e\u0432\u0430\u0440\u0430','\u0422\u043e\u0432\u0430\u0440','\u0421\u0430\u0439\u0442','\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u043d\u0430 \u0441\u0430\u0439\u0442\u0435','\u041f\u0440\u043e\u0438\u0437\u0432\u043e\u0434\u0438\u0442\u0435\u043b\u044c','\u0426\u0435\u043d\u0430 (\u0441\u0443\u043c)',
                   '\u0421\u0442\u0430\u0442\u0443\u0441 \u0441\u043e\u0432\u043f\u0430\u0434\u0435\u043d\u0438\u044f','\u041e\u0446\u0435\u043d\u043a\u0430','\u041f\u0440\u0438\u0447\u0438\u043d\u044b','\u0414\u0430\u0442\u0430','\u0421\u0441\u044b\u043b\u043a\u0430']
        for col, h in enumerate(headers, 1):
            cell = ws.cell(1, col, h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='center')
        ws.row_dimensions[1].height = 20
        for entry in store.report(run_id)['products']:
            for offer in entry['offers']:
                row = [entry['product']['id'], entry['product']['name'], offer['site'],
                       offer['name'], offer.get('manufacturer',''),
                       offer.get('manual_price') or offer.get('observed_price', offer['price']),
                       offer['match_status'], round(offer['score'], 2),
                       ' | '.join(offer['reasons']), offer.get('observed_at','')[:10], offer['url']]
                ws.append(["'" + v if isinstance(v, str) and v.lstrip().startswith(('=','+','-','@')) else v for v in row])
        for col, width in zip(range(1, 12), [8, 40, 10, 45, 25, 14, 16, 8, 50, 12, 50]):
            ws.column_dimensions[get_column_letter(col)].width = width
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return Response(buf.read(), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': 'attachment; filename="dori-candidates.xlsx"'})

    # ------------------------------------------------------------------ 03 Dorixona internal API --

    @app.get('/api/dorixona/stores')
    def dorixona_stores():
        """Return stores currently in pharmacy_stock (after a sync)."""
        return {'items': store.stores()}

    @app.post('/api/dorixona/sync')
    async def dorixona_sync():
        """Pull stores + stock from the internal API, deduplicate, and import."""
        from .dorixona_api import fetch_stores, fetch_all_stock, build_import, TOKEN
        from .adapters import now as _now
        if not TOKEN:
            raise HTTPException(503, 'DORIXONA_SERVICE_TOKEN is not set on the server. Add it to .env.')
        try:
            stores = await fetch_stores()
        except Exception as exc:
            raise HTTPException(502, f'Could not fetch stores: {exc}') from exc
        try:
            stock_by_store = await fetch_all_stock(stores)
        except Exception as exc:
            raise HTTPException(502, f'Could not fetch stock: {exc}') from exc

        products, pharmacy_stock = build_import(stores, stock_by_store)
        synced_at = _now()
        ids = store.import_from_dorixona(products, pharmacy_stock, synced_at)
        return {
            'stores': len(stores),
            'unique_products': len(products),
            'pharmacy_stock_rows': len(pharmacy_stock),
            'product_ids': ids,
            'synced_at': synced_at,
        }

    @app.get('/api/dorixona/stock')
    def dorixona_stock(product_ids: str = ''):
        """Return pharmacy stock rows for given comma-separated dori product ids."""
        if not product_ids:
            return {'items': []}
        try:
            ids = [int(x) for x in product_ids.split(',') if x.strip()]
        except ValueError:
            raise HTTPException(400, 'product_ids must be comma-separated integers')
        grouped = store.pharmacy_stock_for_products(ids)
        return {'items': grouped}

    return app
