"""Browser adapters. No invented APIs, challenge bypass or synthetic fallback prices."""
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


class SourceError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def price_value(text):
    text = text.replace('\xa0', ' ').replace('\u202f', ' ')
    m = re.search(r'(\d[\d ]*(?:[.,]\d{1,2})?)\s*(?:с[ўу]м|so.m|uzs)', text, re.I)
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
    def __init__(self):
        self.delay = max(2, float(os.getenv('CRAWL_DELAY_SECONDS', '3')))
        self.max_pages = max(1, min(10, int(os.getenv('CRAWL_MAX_PAGES', '3'))))
        self.robots = {}
        self.last_request = {}

    async def __aenter__(self):
        self.playwright = await async_playwright().start()
        options = {'headless': True}
        if os.getenv('DORI_BROWSER_PROXY'):
            options['proxy'] = {'server': os.environ['DORI_BROWSER_PROXY']}
        try:
            options['headless'] = os.getenv('DORI_HEADLESS', 'true').lower() != 'false'
            profile = os.getenv('DORI_BROWSER_PROFILE')
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
        await self.context.route('**/*', lambda route: route.abort() if route.request.resource_type in
                                 ('image', 'media', 'font') else route.continue_())
        return self

    async def __aexit__(self, *exc):
        await self.context.close()
        if self.browser:
            await self.browser.close()
        await self.playwright.stop()

    async def allowed(self, url):
        root = f'{urlparse(url).scheme}://{urlparse(url).netloc}'
        if root not in self.robots:
            cookies = httpx.Cookies()
            for cookie in await self.context.cookies(root):
                cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie['path'])
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, cookies=cookies) as client:
                try:
                    response = await client.get(root + '/robots.txt', headers={'User-Agent': 'DoriPriceBot/0.1'})
                    if response.status_code == 404:
                        content = ''
                    elif response.status_code != 200 or '<html' in response.text.lower() or '<!doctype' in response.text.lower():
                        raise SourceError('robots.txt inaccessible or challenged; cannot verify crawl permission')
                    else:
                        content = response.text
                except httpx.HTTPError as exc:
                    raise SourceError('robots.txt request failed: ' + type(exc).__name__) from exc
            parser = RobotFileParser()
            parser.parse(content.splitlines())
            self.robots[root] = parser
        parser = self.robots[root]
        if not parser.can_fetch('DoriPriceBot', url):
            raise SourceError('robots.txt disallows this search path')
        return max(self.delay, parser.crawl_delay('DoriPriceBot') or parser.crawl_delay('*') or 0)

    async def navigate(self, page, url):
        delay = await self.allowed(url)
        host = urlparse(url).hostname
        loop = asyncio.get_running_loop()
        await asyncio.sleep(max(0, self.last_request.get(host, 0) + delay - loop.time()))
        self.last_request[host] = loop.time()
        response = None
        for attempt in range(2):
            response = await page.goto(url, wait_until='domcontentloaded', timeout=60000)
            if not response or response.status not in (502, 503, 504) or attempt == 1:
                break
            await asyncio.sleep(delay * 2)
            self.last_request[host] = loop.time()
        if response and response.status in (401, 403, 429):
            raise SourceError(f'HTTP {response.status}: source blocked or rate limited')
        body = (await page.locator('body').inner_text()).casefold()
        if any(t in body for t in ('access forbidden', 'security detection', 'verify you are human', 'captcha')):
            raise SourceError('Source access challenge; no bypass attempted')
        if response and response.status >= 400:
            raise SourceError(f'HTTP {response.status}')

    async def search(self, site, product):
        from .matching import search_term
        page = await self.context.new_page()
        try:
            if site == 'arzon':
                params = {'barcode': product['barcode']} if product.get('barcode') else {'q': search_term(product)}
                url = 'https://arzonapteka.uz/ru/search?' + urlencode(params)
                await self.navigate(page, url)
                try:
                    await page.wait_for_selector('article[data-product-card]', timeout=15000)
                except Exception:
                    body = (await page.locator('body').inner_text()).casefold()
                    if any(t in body for t in ('ничего не найдено', 'не нашли', 'нет результатов', 'ничего не нашли')):
                        return [], {'status': 'no_results', 'url': page.url, 'pages': 1}
                    raise SourceError('Search cards not found; page may be blocked or markup changed')
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
                if not offers:
                    raise SourceError('Cards found but no verifiable UZS prices parsed')
                offers = list({(o['url'], o['price']): o for o in offers}.values())
                return offers, {'status': 'ok', 'url': url, 'pages': pages,
                                'scope': 'Advertised starting prices; bounded search, not all pharmacy offers'}
            if site != 'oson':
                raise ValueError('Unknown site')
            url = 'https://osonapteka.uz/'
            await self.navigate(page, url)
            search = page.locator('input[type="search"], input[placeholder*="поиск"], input[placeholder*="вводить"]').first
            if not await search.count():
                raise SourceError('Oson search input unavailable; live markup not verified')
            await search.fill(search_term(product))
            await search.press('Enter')
            await page.wait_for_timeout(3000)
            await self.allowed(page.url)
            offers = parse_oson_jsonld(await page.content(), page.url)
            if not offers and os.getenv('OPENAI_PARSE_HTML', 'false').lower() == 'true':
                from .ai import parse_visible_offers
                offers = await parse_visible_offers(await page.locator('body').inner_text(), page.url, 'oson')
            if not offers:
                raise SourceError('Oson search has no supported structured offers; adapter needs live access verification')
            return offers, {'status': 'ok', 'url': page.url, 'pages': 1, 'scope': 'Structured UZS offers'}
        finally:
            await page.close()
