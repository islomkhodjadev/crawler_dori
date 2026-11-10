import asyncio
import os
import csv
import io
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


class CrawlRequest(BaseModel):
    product_ids: list[int] = Field(min_length=1, max_length=5000)
    sites: list[str] = Field(default_factory=lambda: ['arzon', 'oson'], min_length=1, max_length=2)
    use_ai: bool = False


class ConnectionRequest(BaseModel):
    product_id: int
    use_ai: bool = False


class ReviewRequest(BaseModel):
    status: str


class ProductRequest(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    manufacturer: str = Field(default='', max_length=300)
    barcode: str = Field(default='', max_length=30)
    external_id: str = Field(default='', max_length=100)
    arzon_id: str = Field(default='', max_length=100)
    oson_id: str = Field(default='', max_length=100)
    invoice_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)


def create_app(db_path=None):
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
        if body.use_ai and not os.getenv('OPENAI_API_KEY'):
            raise HTTPException(400, 'Configure OPENAI_API_KEY before enabling AI parsing')
        try:
            browsers.require_ready(body.sites)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        products = store.products(list(set(body.product_ids)))
        if len(products) != len(set(body.product_ids)):
            raise HTTPException(404, 'Some products do not exist')
        try:
            run_id = store.create_run(products)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        task = asyncio.create_task(crawl(store, run_id, products, tuple(dict.fromkeys(body.sites)),
                                         admin=browsers, use_ai=body.use_ai))
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
        if body.use_ai and not os.getenv('OPENAI_API_KEY'):
            raise HTTPException(400, 'Configure OPENAI_API_KEY to enable AI parsing')
        products = store.products([body.product_id])
        if not products:
            raise HTTPException(404, 'Select an imported product for the connection test')
        try:
            return await browsers.check(site, products[0], use_ai=body.use_ai)
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
        store.update_run(run_id, status='interrupted', finished_at=now(), error='Collection cancelled by admin')
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
                         'estimate_uzs','estimate_status','invoice_delta_percent','arzon_status','oson_status','sources'])
        for entry in report['products']:
            p, e = entry['product'], entry['estimate']
            row = [p['id'], p['name'], p['manufacturer'], p.get('invoice_price'), e['per_site'].get('arzon'),
                   e['per_site'].get('oson'), e['value'], e['status'], e['invoice_delta_percent'],
                   entry['sources'].get('arzon', {}).get('status'), entry['sources'].get('oson', {}).get('status'),
                   ' | '.join(o['url'] for o in entry['offers'] if o['match_status'] == 'accepted')]
            # Prevent spreadsheet formula execution from untrusted names/manufacturers.
            writer.writerow(["'" + v if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')) else v for v in row])
        return Response('\ufeff' + out.getvalue(), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="dori-prices.csv"'})

    return app
