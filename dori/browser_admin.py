"""Server-owned Chromium sessions, interactive viewer and human-verification handoff."""
import asyncio
import base64
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .adapters import BrowserCrawler, SourceError, VerificationRequired, now

SITES = {'arzon': 'https://arzonapteka.uz/ru', 'oson': 'https://osonapteka.uz/'}
KEYS = {'Enter','Tab','Backspace','Delete','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown',
        'Home','End','PageUp','PageDown','Space','Control+A','Meta+A','Shift+Tab'}


@dataclass
class Session:
    crawler: BrowserCrawler
    page: object
    status: str = 'unchecked'
    message: str = 'Complete verification in the browser, then check connection.'
    checked_at: str | None = None
    checked_monotonic: float = 0
    busy: bool = False
    viewer: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ready: asyncio.Event = field(default_factory=asyncio.Event)


class BrowserAdmin:
    def __init__(self, profile_root=None):
        self.profile_root = Path(profile_root or os.getenv('DORI_ADMIN_PROFILES', 'data/browser-profiles'))
        self.sessions = {}
        self.open_lock = asyncio.Lock()
        self.collecting = False

    def state(self):
        for session in self.sessions.values():
            if session.status == 'connected' and not self.collecting and time.monotonic() - session.checked_monotonic > 900:
                session.status = 'unchecked'
                session.message = 'Connection check expired. Check again before starting.'
                session.ready.clear()
        return {site: ({'status': s.status, 'message': s.message, 'checked_at': s.checked_at,
                        'url': s.page.url, 'busy': s.busy, 'opened': True}
                       if (s := self.sessions.get(site)) else
                       {'status': 'closed', 'message': 'Open the browser to begin.', 'opened': False, 'busy': False})
                for site in SITES}

    async def open(self, site):
        if site not in SITES:
            raise ValueError('Unknown website')
        async with self.open_lock:
            if site in self.sessions:
                return self.sessions[site]
            crawler = BrowserCrawler(profile=str(self.profile_root / site), interactive=True)
            await crawler.__aenter__()
            try:
                page = crawler.context.pages[0] if crawler.context.pages else await crawler.context.new_page()
                await page.set_viewport_size({'width':1280, 'height':800})
                async def restrict_navigation(route):
                    req = route.request
                    if req.is_navigation_request() and req.frame == page.main_frame:
                        host = urlparse(req.url).hostname or ''
                        root = urlparse(SITES[site]).hostname
                        if host != root and not host.endswith('.' + root):
                            await route.abort()
                            return
                    await route.continue_()
                await page.route('**/*', restrict_navigation)
                session = Session(crawler, page)
                self.sessions[site] = session
                try:
                    # This is human navigation, not an automated crawl. Crawl permission
                    # and actual product extraction are checked separately below.
                    await page.goto(SITES[site], wait_until='domcontentloaded', timeout=60000)
                except Exception as exc:
                    session.message = 'Page could not finish loading: ' + type(exc).__name__
                return session
            except BaseException:
                await crawler.__aexit__()
                raise

    async def check(self, site, product, use_ai=False):
        session = self.sessions.get(site)
        if not session:
            raise ValueError('Open this website browser first')
        if session.busy or session.lock.locked() or (self.collecting and session.status == 'connected'):
            raise ValueError('This browser is collecting prices; wait for a verification pause')
        async with session.lock:
            session.busy = True
            try:
                # Check the real product path in the existing verified session. Never
                # treat a loaded homepage or clicked checkbox as proof prices work.
                session.crawler.robots.clear()
                offers, details = await session.crawler.search(site, product, page=session.page, use_ai=use_ai)
                if not offers or details['status'] != 'ok':
                    from .adapters import NoResults
                    raise NoResults('Search returned no priced products. Choose another test product; human verification is not required.')
                session.status = 'connected'
                session.checked_at = now()
                session.checked_monotonic = time.monotonic()
                method=details.get('extraction_method','page_parser')
                session.message = f'Connection verified: {len(offers)} priced listing(s) via {method}.'
                if details.get('warning'):
                    session.message += ' '+details['warning']
                session.ready.set()
                return {'ok': True, 'listings': len(offers), 'checked_at': session.checked_at,
                        'method':method,'warning':details.get('warning'),
                        'preview':[{'name':o.get('name',''),'price':o.get('price'),'currency':o.get('currency','UZS')} for o in offers[:3]]}
            except Exception as exc:
                code=getattr(exc,'code','source_error')
                session.status = 'needs_attention' if isinstance(exc,VerificationRequired) else code
                session.message = str(exc)[:600]
                session.checked_monotonic = 0
                session.ready.clear()
                return {'ok': False, 'error': session.message, 'state':session.status,
                        'human_required':isinstance(exc,VerificationRequired)}
            finally:
                session.busy = False

    def require_ready(self, sites):
        for site in sites:
            s = self.sessions.get(site)
            if not s or s.busy or s.lock.locked() or s.status != 'connected' or time.monotonic() - s.checked_monotonic > 900:
                raise ValueError(f'Open {site} and check its connection before collecting (checks expire after 15 minutes)')

    async def search(self, site, product, use_ai=False):
        s = self.sessions[site]
        async with s.lock:
            s.busy = True
            try:
                offers, details = await s.crawler.search(site, product, page=s.page, use_ai=use_ai)
                for offer in offers:
                    offer['location'] = 'Browser session region; verify selected city in the live viewer'
                return offers, details
            except SourceError as exc:
                s.status='needs_attention' if isinstance(exc,VerificationRequired) else exc.code
                s.message=str(exc)[:600]
                s.checked_monotonic=0
                raise
            finally:
                s.busy = False

    async def wait_for_human(self, site, message):
        s = self.sessions[site]
        s.status = 'needs_attention'
        s.message = message[:600] + ' Complete verification, then Check connection to resume.'
        s.checked_monotonic = 0
        s.ready.clear()
        await s.ready.wait()

    async def input(self, site, command):
        s = self.sessions.get(site)
        if not s:
            raise ValueError('Browser not opened')
        if s.busy or s.lock.locked() or (self.collecting and s.status == 'connected'):
            raise ValueError('Browser is controlled by the crawler. Human input is available while paused.')
        async with s.lock:
            kind = command.get('type')
            if kind == 'click':
                x, y = float(command['x']), float(command['y'])
                if not (0 <= x <= 1280 and 0 <= y <= 800):
                    raise ValueError('Click outside browser viewport')
                await s.page.mouse.click(x, y)
            elif kind == 'text':
                value = command.get('text', '')
                if not isinstance(value, str) or len(value) > 2000:
                    raise ValueError('Text input is too long')
                await s.page.keyboard.insert_text(value)
            elif kind == 'key' and command.get('key') in KEYS:
                await s.page.keyboard.press(command['key'])
            elif kind == 'scroll':
                delta = max(-1200, min(1200, float(command.get('delta', 0))))
                await s.page.mouse.wheel(0, delta)
            elif kind == 'reload':
                s.crawler.robots.clear()
                await s.page.reload(wait_until='domcontentloaded', timeout=60000)
            elif kind == 'home':
                await s.page.goto(SITES[site], wait_until='domcontentloaded', timeout=60000)
            else:
                raise ValueError('Unsupported browser input')
            s.status = 'unchecked'
            s.checked_monotonic = 0
            s.ready.clear()
            s.message = 'Browser changed. Check connection when verification is complete.'

    async def close(self):
        await asyncio.gather(*(session.crawler.__aexit__() for session in self.sessions.values()), return_exceptions=True)
        self.sessions.clear()

    async def stream(self, websocket, site):
        session = self.sessions.get(site)
        if not session or session.viewer:
            await websocket.close(code=1008, reason='Open browser first; only one controller is allowed per website')
            return
        session.viewer = True
        await websocket.accept()
        async def frames():
            while True:
                try:
                    frame = await session.page.screenshot(type='jpeg', quality=65, timeout=5000)
                    await websocket.send_json({'type':'frame', 'image':base64.b64encode(frame).decode(),
                                               'url':session.page.url, 'width':1280, 'height':800})
                except Exception:
                    await websocket.send_json({'type':'notice','message':'Browser is navigating; waiting for the next frame.'})
                await asyncio.sleep(.7)
        async def events():
            while True:
                command = await websocket.receive_json()
                try:
                    if not isinstance(command, dict):
                        raise ValueError('Invalid browser command')
                    await self.input(site, command)
                except Exception as exc:
                    await websocket.send_json({'type':'notice','message':str(exc)[:300]})
        jobs = [asyncio.create_task(frames()), asyncio.create_task(events())]
        try:
            await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            session.viewer = False
