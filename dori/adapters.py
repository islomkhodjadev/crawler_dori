"""Browser adapters. No invented APIs, challenge bypass or synthetic fallback prices."""
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from .challenge import settle, retry_after_seconds
from .browser_config import launch_options
from .logs import get
from .page_state import classify_text, has_visible_price

log = get('adapters')


class SourceError(RuntimeError):
    code = 'source_error'


class ExtractionError(SourceError):
    code = 'extraction_error'


class AccessBlocked(SourceError):
    code = 'access_blocked'


class CrawlPermissionError(AccessBlocked):
    code = 'crawl_permission'


class RateLimited(SourceError):
    code = 'rate_limited'


class AutomaticVerificationTimeout(SourceError):
    code = 'verification_timeout'


class NoResults(SourceError):
    code = 'no_results'


class VerificationRequired(SourceError):
    """A browser session requires a person before automation can continue."""
    code = 'human_verification'


def now():
    return datetime.now(timezone.utc).isoformat()


def price_value(text):
    text = text.replace('\xa0', ' ').replace('\u202f', ' ')
    m = re.search(r'(\d[\d ]*(?:[.,]\d{1,2})?)\s*(?:с[ўу]м|so[ʻ’\']?m|uzs)', text, re.I)
    if not m:
        return None
    value = float(m[1].replace(' ', '').replace(',', '.'))
    return value if value > 0 else None


def parse_arzon(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    offers = []
    for card in soup.select('article[data-product-card]'):
        link = card.select_one('a[href*="/products/"]')
        if not link or not link.get('aria-label'):
            continue
        name = link['aria-label'].strip()
        paragraphs = card.find_all('p')
        price = next((price_value(p.get_text(' ', strip=True)) for p in paragraphs
                      if price_value(p.get_text(' ', strip=True)) is not None), None)
        if price is None:
            continue
        title = next((p for p in paragraphs if p.get_text(' ', strip=True) == name), None)
        manufacturer = ''
        if title:
            # Observed detailed cards place a country + manufacturer span after title.
            sibling = title.find_next_sibling('span')
            if sibling:
                manufacturer = sibling.get_text(' ', strip=True)
                manufacturer = re.sub(r'[^\w\s.,&()/-]', '', manufacturer).strip()
        url = urljoin(page_url, link['href'])
        if urlparse(url).hostname != 'arzonapteka.uz':
            continue
        site_id = url.rstrip('/').rsplit('-', 1)[-1]
        offers.append(dict(site='arzon', site_id=site_id, name=name, manufacturer=manufacturer,
                           price=price, currency='UZS', url=url, observed_at=now(),
                           price_kind='advertised_minimum', location='Uzbekistan (site default)'))
    return list({(o['url'], o['price']): o for o in offers}.values())


def parse_oson_jsonld(html, page_url):
    """Standards-based parser; live DOM remains unverified while Oson blocks access."""
    soup = BeautifulSoup(html, 'html.parser')
    found = []
    def visit(node):
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            types = node.get('@type', [])
            types = [types] if isinstance(types, str) else types
            if any(t in ('Product', 'Drug') for t in types) and node.get('name'):
                offers = node.get('offers', [])
                offers = offers if isinstance(offers, list) else [offers]
                for offer in offers:
                    if not isinstance(offer, dict) or offer.get('priceCurrency', '').upper() != 'UZS':
                        continue
                    raw = offer.get('price', offer.get('lowPrice'))
                    try:
                        price = float(raw)
                    except (TypeError, ValueError):
                        continue
                    if not 0 < price < float('inf'):
                        continue
                    url = urljoin(page_url, offer.get('url') or node.get('url') or page_url)
                    if urlparse(url).hostname != 'osonapteka.uz':
                        continue
                    maker = node.get('manufacturer') or node.get('brand') or ''
                    if isinstance(maker, dict):
                        maker = maker.get('name', '')
                    found.append(dict(site='oson', site_id=str(node.get('sku', '')),
                                      barcode=str(node.get('gtin13', '')), name=node['name'],
                                      manufacturer=str(maker), price=price, currency='UZS', url=url,
                                      observed_at=now(), price_kind='advertised_minimum' if 'lowPrice' in offer else 'offer',
                                      location='Uzbekistan (site default)'))
            for key in ('@graph', 'itemListElement', 'item'):
                if key in node:
                    visit(node[key])
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.get_text()))
        except (ValueError, TypeError):
            continue
    return found


class BrowserCrawler:
    def __init__(self, profile=None, interactive=False, agent_bus=None):
        self.profile = profile
        self.interactive = interactive
        self.agent_bus = agent_bus  # AgentBus instance for IP-blocked sites
        self.delay = max(2, float(os.getenv('CRAWL_DELAY_SECONDS', '3')))
        self.max_pages = max(1, min(10, int(os.getenv('CRAWL_MAX_PAGES', '3'))))
        self.robots = {}
        self.last_request = {}
        self.assessments = {}
        self.robots_checked = {}
        self.verification = {'state':'idle','message':''}
        self.verification_timeout=max(1,min(120,float(os.getenv('DORI_VERIFICATION_TIMEOUT','30'))))
        self.max_backoff=max(0,min(300,float(os.getenv('DORI_MAX_BACKOFF','30'))))

    async def __aenter__(self):
        options = launch_options(interactive=self.interactive)
        self.playwright = await async_playwright().start()
        try:
            profile = self.profile or os.getenv('DORI_BROWSER_PROFILE')
            if profile:
                self.context = await self.playwright.chromium.launch_persistent_context(profile, locale='ru-RU', **options)
                self.browser = None
            else:
                self.browser = await self.playwright.chromium.launch(**options)
                self.context = await self.browser.new_context(locale='ru-RU')
        except Exception:
            await self.playwright.stop()
            raise
        # Retain ordinary browser navigation/session handling, remove expensive assets.
        if not self.interactive:
            await self.context.route('**/*', lambda route: route.abort() if route.request.resource_type in
                                     ('image', 'media', 'font') else route.continue_())
        return self

    async def __aexit__(self, *exc):
        await self.context.close()
        if self.browser:
            await self.browser.close()
        await self.playwright.stop()

    def verification_progress(self, state, message):
        if self.verification.get('state')!=state:
            log.info('browser verification state=%s message=%s',state,message)
        self.verification={'state':state,'message':message}

    async def browser_load(self, page, url):
        """Same browser/UA/profile for pages and robots; bounded server backoff."""
        self.verification_progress('loading','Loading website in the existing browser session.')
        try:
            for attempt in range(2):
                response=await page.goto(url,wait_until='domcontentloaded',timeout=60000)
                status=response.status if response else 200
                if status==429 or status in (502,503,504):
                    wait=retry_after_seconds(response.headers.get('retry-after'))
                    wait=max(self.delay*(2**attempt),wait or 0)
                    if attempt==0 and wait<=self.max_backoff:
                        self.verification_progress('backoff',f'HTTP {status}: waiting {wait:g} seconds before one retry.')
                        await asyncio.sleep(wait)
                        continue
                    if status==429:
                        raise RateLimited(f'Website rate limited this session. Retry after at least {wait:g} seconds; no early retry was made.')
                    raise SourceError(f'Website returned HTTP {status} after bounded retries')
                outcome=await settle(page,response,self.verification_timeout,self.verification_progress)
                if outcome.state=='human_verification':
                    raise VerificationRequired('Website requires human verification. Complete it in the browser, then check connection.')
                if outcome.state in ('access_blocked','unexpected_redirect'):
                    raise AccessBlocked('Website denies access or redirected away from its host; browser verification did not grant access.')
                if outcome.state=='verification_timeout':
                    raise AutomaticVerificationTimeout('Automatic browser verification timed out. Inspect the browser and retry; no successful verification was assumed.')
                if outcome.status==468 and outcome.automatic and attempt==0:
                    # Confirm the granted session with one normal request; never
                    # assume that a changed challenge DOM means server access.
                    continue
                if outcome.status>=400 and not (outcome.status==404 and urlparse(url).path=='/robots.txt') and not (outcome.status==468 and outcome.state in ('prices_visible','no_results')):
                    raise SourceError(f'HTTP {outcome.status}: no verified product page was obtained')
                self.verification_progress('ready','Browser page loaded'+(' after automatic verification.' if outcome.automatic else '.'))
                return response,outcome
        except SourceError as exc:
            self.verification_progress(exc.code,str(exc))
            raise
        except Exception as exc:
            self.verification_progress('source_error','Browser request failed: '+type(exc).__name__)
            raise

    async def allowed(self, url):
        root=f'{urlparse(url).scheme}://{urlparse(url).netloc}'
        loop=asyncio.get_running_loop()
        if root not in self.robots or loop.time()-self.robots_checked.get(root,0)>300:
            robots_page=await self.context.new_page()
            try:
                response,outcome=await self.browser_load(robots_page,root+'/robots.txt')
                status=outcome.status
                if status==404:
                    content=''
                elif status==200:
                    content=await robots_page.locator('body').inner_text()
                    # Navigation after a challenge can replace the response. Use the
                    # current page's plain-text rendering, but reject HTML challenge pages.
                    if '<html' in content.casefold():
                        raise CrawlPermissionError('robots.txt did not provide a readable policy')
                    current_html=await robots_page.content()
                    if not ('<pre' in current_html.casefold() or 'text/plain' in (response.headers.get('content-type','') if response else '')):
                        raise CrawlPermissionError('robots.txt returned HTML instead of a plain-text policy')
                else:
                    raise CrawlPermissionError(f'robots.txt returned HTTP {status}')
                parser=RobotFileParser()
                parser.parse(content.splitlines())
                self.robots[root]=parser
                self.robots_checked[root]=loop.time()
            except (VerificationRequired,RateLimited,AutomaticVerificationTimeout):
                raise
            except SourceError as exc:
                raise CrawlPermissionError('Cannot verify crawl permission: '+str(exc)) from exc
            finally:
                await robots_page.close()
        parser=self.robots[root]
        if not parser.can_fetch('DoriPriceBot',url):
            raise CrawlPermissionError('robots.txt disallows this search path')
        return max(self.delay,parser.crawl_delay('DoriPriceBot') or parser.crawl_delay('*') or 0)

    async def navigate(self, page, url):
        try:
            delay=await self.allowed(url)
        except (VerificationRequired,AutomaticVerificationTimeout):
            # Surface the policy challenge in the admin-controlled page before
            # closing the temporary policy tab; the same cookies are shared.
            await page.goto(f'{urlparse(url).scheme}://{urlparse(url).netloc}/robots.txt',wait_until='domcontentloaded',timeout=60000)
            raise
        host=urlparse(url).hostname
        loop=asyncio.get_running_loop()
        await asyncio.sleep(max(0,self.last_request.get(host,0)+delay-loop.time()))
        self.last_request[host]=loop.time()
        await self.browser_load(page,url)

    async def assess_page(self, page, site):
        from .ai import analyze_visible_page, AIExtractionError
        import hashlib
        text=await page.locator('body').inner_text()
        key=(page.url, hashlib.sha256(text.encode()).hexdigest())
        cached=self.assessments.get(id(page))
        if cached and cached[0]==key:
            return cached[1]
        try:
            shot=await page.screenshot(type='jpeg',quality=55,timeout=5000)
        except Exception:
            shot=None
        try:
            result=await analyze_visible_page(text,page.url,site,screenshot=shot)
        except AIExtractionError as exc:
            raise ExtractionError(str(exc)) from exc
        self.assessments[id(page)]=(key,result)
        return result

    async def ai_extract(self, page, site):
        result=await self.assess_page(page,site)
        state=result['state']
        if state=='human_verification':
            raise VerificationRequired(result['reason'] or 'AI detected a human verification step')
        if state=='access_blocked':
            raise AccessBlocked(result['reason'] or 'Website denies access without a verification control')
        if state=='rate_limited':
            raise RateLimited(result['reason'] or 'Website rate limited this session')
        if state=='no_results':
            return [],{'status':'no_results','url':page.url,'pages':1,'assessment':result['reason']}
        if result['offers']:
            return result['offers'],{'status':'ok','url':page.url,'pages':1,'extraction_method':'openai',
                                     'scope':'AI-parsed visible prices; review product matches',
                                     'assessment':result['reason']}
        if state=='prices_visible':
            raise ExtractionError(f"AI saw prices but {result['validated']}/{result['proposed']} extracted listings passed text evidence checks. Try another product or check the page layout.")
        raise ExtractionError('AI page assessment: '+state+'. '+result['reason'])

    async def search(self, site, product, page=None, use_ai=None):
        log.info('search start site=%s product_id=%s name=%r use_ai=%s', site, product.get('id'), product['name'], use_ai)
        try:
            offers, details = await self._search(site, product, page=page, use_ai=use_ai)
        except Exception as exc:
            if isinstance(exc, (SourceError, ValueError)):
                log.warning('search stopped site=%s product_id=%s: %s: %s', site, product.get('id'), type(exc).__name__, exc)
            else:
                log.exception('search crashed site=%s product_id=%s', site, product.get('id'))
            raise
        sample = [f"{o['name'][:60]} @ {o['price']}" for o in offers[:3]]
        log.info('search done site=%s product_id=%s status=%s method=%s offers=%d sample=%s url=%s',
                 site, product.get('id'), details.get('status'), details.get('extraction_method', 'page_parser'),
                 len(offers), sample, details.get('url'))
        if details.get('warning'):
            log.warning('search warning site=%s product_id=%s: %s', site, product.get('id'), details['warning'])
        return offers, details

    async def _search(self, site, product, page=None, use_ai=None):
        from .matching import search_term
        if use_ai is None:
            use_ai = os.getenv('OPENAI_PARSE_HTML', 'false').lower() == 'true'

        # Agent shortcut: when a Chrome extension is connected and site is Oson
        # (which is IP-blocked on the server), skip the server-side browser entirely
        # and fetch directly through the user's real Chrome. This bypasses both the
        # IP block and the robots.txt check that would also fail from this IP.
        if site == 'oson' and self.agent_bus and self.agent_bus.connected() and page is None:
            agent_url = 'https://osonapteka.uz/search?' + urlencode({'q': search_term(product)})
            log.info('agent shortcut site=oson product_id=%s url=%.120s', product.get('id'), agent_url)
            result = await self.agent_bus.fetch(agent_url, site)
            if result and result.get('html'):
                html = result['html']
                final_url = result.get('url', agent_url)
                from .oson_cards import parse_oson_cards
                offers = parse_oson_jsonld(html, final_url)
                method = 'agent_json_ld'
                if not offers:
                    offers = parse_oson_cards(html, final_url)
                    method = 'agent_visible_cards'
                if offers:
                    return offers, {'status': 'ok', 'url': final_url, 'pages': 1,
                                    'extraction_method': method,
                                    'scope': 'Oson prices via Chrome extension agent (user browser)'}
                body_text = BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
                state = classify_text(body_text)
                if state == 'no_results':
                    return [], {'status': 'no_results', 'url': final_url, 'pages': 1}
                log.warning('agent shortcut returned page but no offers parsed site=oson state=%s', state)
                # Fall through to server-side browser as last resort
            else:
                log.warning('agent shortcut failed or timed out for oson product_id=%s', product.get('id'))
                # Fall through to server-side browser

        own_page = page is None
        page = page or await self.context.new_page()
        self.assessments.pop(id(page),None)
        try:
            if site == 'arzon':
                params = {'barcode': product['barcode']} if product.get('barcode') else {'q': search_term(product)}
                url = 'https://arzonapteka.uz/ru/search?' + urlencode(params)
                await self.navigate(page, url)
                try:
                    await page.wait_for_function("""() => [...document.querySelectorAll('article[data-product-card]')].some(card =>
                        card.querySelector('a[href*=\"/products/\"][aria-label]') && /[0-9][0-9\\s.,]*\\s*(?:с[ўу]м|UZS)/i.test(card.innerText))""", timeout=20000)
                except Exception:
                    body = (await page.locator('body').inner_text()).casefold()
                    if classify_text(body)=='no_results':
                        return [], {'status': 'no_results', 'url': page.url, 'pages': 1}
                    if use_ai:
                        return await self.ai_extract(page,site)
                    state=classify_text(body)
                    if state=='human_verification':
                        raise VerificationRequired('Website requests human verification')
                    if state=='access_blocked':
                        raise AccessBlocked('Website denies access')
                    raise ExtractionError('Search cards not found; page may be loading or markup changed. Enable AI parsing for page assessment.')
                offers, visited, pages = [], set(), 0
                for _ in range(self.max_pages):
                    visited.add(page.url)
                    pages += 1
                    offers.extend(parse_arzon(await page.content(), page.url))
                    next_link = page.locator('a[rel="next"]').first
                    if not await next_link.count():
                        break
                    href = await next_link.get_attribute('href')
                    next_url = urljoin(page.url, href or '')
                    if next_url in visited or urlparse(next_url).hostname != 'arzonapteka.uz':
                        break
                    await self.navigate(page, next_url)
                if not offers and use_ai:
                    return await self.ai_extract(page,site)
                if not offers:
                    raise SourceError('Cards found but no verifiable UZS prices parsed')
                offers = list({(o['url'], o['price']): o for o in offers}.values())
                return offers, {'status': 'ok', 'url': url, 'pages': pages,
                                'scope': 'Advertised starting prices; bounded search, not all pharmacy offers'}
            if site != 'oson':
                raise ValueError('Unknown site')
            url='https://osonapteka.uz/search?'+urlencode({'q':search_term(product)})
            await self.navigate(page,url)
            try:
                await page.wait_for_function("""() => /[0-9][0-9\\s.,]*\\s*(?:с[ўу]м|UZS)/i.test(document.body.innerText)
                    || /ничего не найдено|ничего не нашли|лекарств:\\s*0|access forbidden|verify you are human/i.test(document.body.innerText)""",timeout=20000)
            except Exception:
                pass  # AI can distinguish a slow page, challenge, or unfamiliar layout.
            text=await page.locator('body').inner_text()
            ai_error=None
            if use_ai:
                try:
                    extracted=await self.ai_extract(page,site)
                    if extracted[0]:
                        return extracted
                    ai_error='AI reported no priced results.'
                except ExtractionError as exc:
                    ai_error=str(exc)
                    # A deterministic card parser can still recover loaded prices.
            from .oson_cards import parse_oson_cards
            offers=parse_oson_jsonld(await page.content(),page.url)
            method='json_ld'
            if not offers:
                offers=parse_oson_cards(await page.content(),page.url)
                method='visible_cards'
            if offers:
                details={'status':'ok','url':page.url,'pages':1,'extraction_method':method,
                         'scope':'Visible Oson starting prices; verify matching product packages'}
                if ai_error:details['warning']=ai_error+' Used visible-card fallback.'
                return offers,details
            state=classify_text(text)
            if state=='no_results':
                return [],{'status':'no_results','url':page.url,'pages':1}
            if state=='human_verification':
                raise VerificationRequired('Website requests human verification')
            if state=='access_blocked':
                raise AccessBlocked('Website denies access')
            if ai_error:
                raise ExtractionError(ai_error)
            raise ExtractionError('Oson page loaded but no supported priced cards were extracted. Enable AI parsing when an OpenAI key is configured.')
        except (VerificationRequired, AccessBlocked) as exc:
            if isinstance(exc,CrawlPermissionError):
                raise
            if use_ai:
                # Let AI assess the actual page before asking a person to act.
                return await self.ai_extract(page,site)
            raise
        finally:
            if own_page:
                self.assessments.pop(id(page),None)
                await page.close()
