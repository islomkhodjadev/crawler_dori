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


async def _fetch_oson_page(agent_bus, query):
    """Fetch one Oson search page via agent and return (offers, html, final_url) or None on failure."""
    from .oson_cards import parse_oson_cards
    agent_url = 'https://osonapteka.uz/search?' + urlencode({'q': query})
    result = await agent_bus.fetch(agent_url, 'oson')
    if not result or not result.get('html'):
        return None
    html = result['html']
    final_url = result.get('url', agent_url)
    offers = parse_oson_jsonld(html, final_url)
    if not offers:
        offers = parse_oson_cards(html, final_url)
    return offers, html, final_url


async def agent_search_oson(agent_bus, product, use_ai=None):
    """Fetch Oson search results through the connected Chrome extension agent.
    If the primary search returns no parseable offers, try AI-generated name variants."""
    from .matching import search_term
    primary_query = search_term(product, site='oson')
    log.info('agent shortcut site=oson product_id=%s query=%r', product.get('id'), primary_query)

    fetched = await _fetch_oson_page(agent_bus, primary_query)
    if fetched is None:
        log.warning('agent shortcut failed or timed out for oson product_id=%s', product.get('id'))
        return [], {'status': 'error', 'error': 'agent fetch timed out or returned no HTML', 'observed_at': now()}

    offers, html, final_url = fetched
    if offers:
        return offers, {'status': 'ok', 'url': final_url, 'pages': 1,
                        'extraction_method': 'agent_json_ld' if parse_oson_jsonld(html, final_url) else 'agent_visible_cards',
                        'scope': 'Oson prices via Chrome extension agent (user browser)'}

    # Primary query returned a page but no offers — try AI-generated variants.
    if use_ai or os.getenv('OPENAI_API_KEY'):
        from .ai import search_term_variants
        variants = await search_term_variants(product, site='oson')
        for variant in variants:
            if variant.lower() == primary_query.lower():
                continue
            log.info('agent shortcut oson fallback product_id=%s variant=%r', product.get('id'), variant)
            fetched2 = await _fetch_oson_page(agent_bus, variant)
            if fetched2 and fetched2[0]:
                v_offers, v_html, v_url = fetched2
                return v_offers, {'status': 'ok', 'url': v_url, 'pages': 1,
                                  'extraction_method': 'agent_json_ld',
                                  'scope': f'Oson via agent (AI variant: {variant})'}

    body_text = BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
    state = classify_text(body_text)
    if state == 'no_results':
        return [], {'status': 'no_results', 'url': final_url, 'pages': 1}
    log.warning('agent shortcut returned page but no offers parsed site=oson state=%s', state)
    return [], {'status': 'no_results', 'url': final_url, 'pages': 1}


def _name_similarity(a, b):
    """Return fraction of words in a that appear in b (case-insensitive)."""
    wa = set(re.findall(r'\w+', a.lower()))
    wb = set(re.findall(r'\w+', b.lower()))
    if not wa:
        return 0.0
    return len(wa & wb) / len(wa)


def parse_arzon_pharmacies(html, product_name, product_url):
    """Parse the /checkout/pharmacies SSR page and return one offer per pharmacy.

    If the cart had only one item (the target product), every price line is for it.
    If somehow multiple items remain, we match by product name proximity.
    """
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text('\n', strip=True)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    offers = []
    i = 0
    while i < len(lines):
        if lines[i].startswith('Адрес:'):
            addr = lines[i][len('Адрес:'):].strip()
            # Pharmacy name: scan backwards for first non-metadata line
            pharmacy_name = ''
            for j in range(i - 1, max(i - 10, -1), -1):
                l = lines[j]
                if re.match(r'^\d{2}:\d{2}|^\d+\s*[чд]$|^Бронир|^Лучш|^Выбор|^Ближе|^Дешевле|^Открыто|^24\/7|^С бронир|^Ориентир', l):
                    continue
                pharmacy_name = l
                break
            # Scan forward to find price for the target product.
            # Pattern: product name line → "1 шт × N сўм" on next line.
            # Fall back to first price if no name match found.
            product_price = None
            last_name_match_score = 0.0
            pending_name_score = 0.0
            for j in range(i + 1, min(i + 40, len(lines))):
                l = lines[j]
                if l in ('Забронировать', 'Выбрать', 'Полная информация'):
                    break
                # Detect a product name line (not a price/metadata)
                if '×' not in l and not re.search(r'\d{2}:\d{2}|Нет в наличии|В наличии|Доступно', l):
                    score = _name_similarity(product_name, l)
                    if score > 0.3:
                        pending_name_score = score
                # Price line: "1 шт × 12 700 сўм"
                if '×' in l and re.search(r'\d[\d\s]+\s*сўм', l):
                    val = price_value(l)
                    if val and val > 0:
                        # Prefer price that follows a matching product name
                        if pending_name_score > last_name_match_score:
                            product_price = val
                            last_name_match_score = pending_name_score
                        elif product_price is None:
                            product_price = val  # first price as fallback
                    pending_name_score = 0.0
            if pharmacy_name and product_price:
                offers.append(dict(
                    site='arzon', site_id='', name=product_name,
                    manufacturer='', price=product_price, currency='UZS',
                    url=product_url, observed_at=now(),
                    price_kind='per_pharmacy',
                    location=addr, pharmacy_name=pharmacy_name, pharmacy_address=addr,
                ))
        i += 1
    return offers


async def agent_search_arzon(agent_bus, product, region_id=2114):
    """Fetch per-pharmacy ArzonApteka prices via Chrome extension.
    Adds the product to cart, loads the pharmacy chooser page, parses pharmacy list, clears cart."""
    from .matching import search_term

    # Build the product detail URL — we need the site_id from the search page first
    # First do a normal agent fetch of the search page to get the product URL
    query = search_term(product, site='arzon')
    search_url = 'https://arzonapteka.uz/ru/search?' + urlencode({'q': query})
    log.info('agent arzon_pharmacies: fetching search page product_id=%s query=%r', product.get('id'), query)
    search_result = await agent_bus.fetch(search_url, 'arzon')
    if not search_result or not search_result.get('html'):
        log.warning('agent arzon_pharmacies: search fetch failed product_id=%s', product.get('id'))
        return [], {'status': 'error', 'error': 'arzon search fetch failed'}

    # Parse search results to get the product page URL
    search_offers = parse_arzon(search_result['html'], search_result.get('url', search_url))
    if not search_offers:
        return [], {'status': 'no_results', 'url': search_url, 'pages': 1}

    # Use the best-matching product URL (first result = most relevant)
    product_url = search_offers[0]['url']
    product_name = search_offers[0]['name']
    log.info('agent arzon_pharmacies: got product_url=%s name=%r', product_url, product_name)

    # Now ask the extension to do the multi-step pharmacy fetch
    result = await agent_bus.fetch_arzon_pharmacies(product_url, region_id)
    if not result or not result.get('html'):
        log.warning('agent arzon_pharmacies: pharmacy fetch failed product_id=%s', product.get('id'))
        return [], {'status': 'error', 'error': 'arzon pharmacy page fetch failed'}

    offers = parse_arzon_pharmacies(result['html'], product_name, product_url)
    if not offers:
        log.warning('agent arzon_pharmacies: no pharmacies parsed product_id=%s url=%s', product.get('id'), result.get('url'))
        # Fall back to the search result prices (site-wide minimum)
        return search_offers, {'status': 'ok', 'url': search_url, 'pages': 1,
                               'extraction_method': 'agent_search_fallback',
                               'scope': 'ArzonApteka minimum prices (pharmacy page parse failed)'}

    log.info('agent arzon_pharmacies: parsed %d pharmacy offers product_id=%s', len(offers), product.get('id'))
    return offers, {'status': 'ok', 'url': result.get('url', product_url), 'pages': 1,
                    'extraction_method': 'agent_pharmacy_page',
                    'scope': f'ArzonApteka per-pharmacy prices (region {region_id})'}


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
            sibling = title.find_next_sibling('span')
            if sibling:
                manufacturer = sibling.get_text(' ', strip=True)
                manufacturer = re.sub(r'[^\w\s.,&()/-]', '', manufacturer).strip()
        url = urljoin(page_url, link['href'])
        if urlparse(url).hostname != 'arzonapteka.uz':
            continue
        site_id = url.rstrip('/').rsplit('-', 1)[-1]

        # Try to extract pharmacy branch name and address from the card.
        # ArzonApteka shows them in spans/divs with address-like selectors.
        pharmacy_name = ''
        pharmacy_address = ''
        for sel in ('span[class*="pharmacy"]', 'div[class*="pharmacy"]', 'span[class*="branch"]',
                    'span[class*="address"]', 'div[class*="address"]', 'span[class*="location"]'):
            el = card.select_one(sel)
            if el:
                text = el.get_text(' ', strip=True)
                if text and len(text) > 3:
                    if 'address' in sel or 'location' in sel:
                        pharmacy_address = text
                    else:
                        pharmacy_name = text
                    break
        # Fallback: look for any small text that looks like a street address (contains "ул.", "str.", district names)
        if not pharmacy_address:
            addr_pattern = re.compile(r'(?:ул\.|улица|str\.|кўча|district|район|тум|шоссе|пр-т|пр\.)', re.I)
            for el in card.find_all(['span', 'div', 'small']):
                text = el.get_text(' ', strip=True)
                if addr_pattern.search(text) and len(text) < 200:
                    pharmacy_address = text
                    break

        location_parts = [p for p in [pharmacy_name, pharmacy_address] if p]
        location = ', '.join(location_parts) if location_parts else 'Узбекистан'

        offers.append(dict(site='arzon', site_id=site_id, name=name, manufacturer=manufacturer,
                           price=price, currency='UZS', url=url, observed_at=now(),
                           price_kind='advertised_minimum', location=location,
                           pharmacy_name=pharmacy_name, pharmacy_address=pharmacy_address))
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
        # and fetch directly through the user's real Chrome.
        if site == 'oson' and self.agent_bus and self.agent_bus.connected() and page is None:
            offers, details = await agent_search_oson(self.agent_bus, product, use_ai=use_ai)
            if details.get('status') in ('ok', 'no_results'):
                return offers, details
            # Fall through to server-side browser as last resort

        # Agent shortcut for ArzonApteka: when user is logged in via extension,
        # use the per-pharmacy checkout page to get prices for each branch in the region.
        if site == 'arzon' and self.agent_bus and self.agent_bus.connected() and page is None:
            region_id = int(os.getenv('ARZON_REGION_ID', '2114'))
            offers, details = await agent_search_arzon(self.agent_bus, product, region_id=region_id)
            if details.get('status') in ('ok', 'no_results'):
                return offers, details
            # Fall through to server-side browser on error

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
