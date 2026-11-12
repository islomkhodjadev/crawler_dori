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
from .storage import Store
from .browser_admin import BrowserAdmin, SITES
from .auth import AdminAuth
from .logs import get, setup as setup_logging

log = get('http')


class CrawlRequest(BaseModel):
    product_ids: list[int] = Field(min_length=1, max_length=5000)
    sites: list[str] = Field(default_factory=lambda: ['arzon', 'oson'], min_length=1, max_length=2)
    use_ai: bool | None = None


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
    auth = AdminAuth()

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
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
                'admin_password_set': bool(auth.password)}

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
    def products():
        return store.products()

    @app.post('/api/products')
    def add_product(body: ProductRequest):
        data = body.model_dump()
        data.update(quantity=None, source_row=None, sheet='Manual')
        ids = store.import_products([data], 'Manual')
        return store.products(ids)[0]

    @app.get('/api/runs')
    def runs():
        return store.runs()

    @app.post('/api/crawl')
    async def start(body: CrawlRequest):
        if not set(body.sites) <= {'arzon', 'oson'}:
            raise HTTPException(400, 'Supported sites: arzon, oson')
        use_ai = body.use_ai if body.use_ai is not None else bool(os.getenv('OPENAI_API_KEY'))
        if use_ai and not os.getenv('OPENAI_API_KEY'):
            raise HTTPException(400, 'Configure OPENAI_API_KEY before enabling AI parsing')
        try:
            browsers.require_ready(body.sites)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        products = store.products(list(set(body.product_ids)))
        if len(products) != len(set(body.product_ids)):
            raise HTTPException(404, 'Some products do not exist')
        try:
            run_id = store.create_run(products, sites=body.sites)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        task = asyncio.create_task(crawl(store, run_id, products, tuple(dict.fromkeys(body.sites)),
                                         admin=browsers, use_ai=use_ai))
        run_tasks[run_id] = task
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(lambda _: run_tasks.pop(run_id, None))
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
            raise HTTPException(503, 'Chromium could not start: ' + type(exc).__name__ + '. Run python -m playwright install chromium.') from exc
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
        task = run_tasks.get(run_id)
        if not task:
            raise HTTPException(404, 'Active run not found')
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        from .adapters import now
        store.update_run(run_id, status='interrupted', finished_at=now(), active_product_id=None, active_site=None, phase='Cancelled', error='Collection cancelled by admin')
        return {'ok': True}

    @app.get('/api/statistics')
    def statistics(run_id: int | None = None):
        return store.statistics(run_id)

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
                     for o in entry['offers'] if o['match_status'] == 'accepted' and o['price'] > 0]
        if not confirmed:
            raise HTTPException(409, 'Нет подтверждённых цен конкурентов — сначала подтвердите совпадения.')
        ours = entry['product'].get('invoice_price')
        prices = [o['price'] for o in confirmed] + ([ours] if ours else [])
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
                         'optimal_price_uzs','optimal_price_reason'])
        for entry in report['products']:
            p, e = entry['product'], entry['estimate']
            row = [p['id'], p['name'], p['manufacturer'], p.get('invoice_price'), e['per_site'].get('arzon'),
                   e['per_site'].get('oson'), e['value'], e['status'], e['invoice_delta_percent'],
                   entry['sources'].get('arzon', {}).get('status'), entry['sources'].get('oson', {}).get('status'),
                   ' | '.join(o['url'] for o in entry['offers'] if o['match_status'] == 'accepted')]
            summary=entry['matching']
            row.append(summary['status'])
            for site in ('arzon','oson'):
                row.extend(summary['per_site'].get(site,{}).get(key,0) for key in ('candidates','accepted','review','rejected'))
            row.append(summary['notes'])
            row.extend([entry['advice']['price'], entry['advice']['reason']] if entry.get('advice') else ['', ''])
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

    return app
