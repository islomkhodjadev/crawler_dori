"""Real Chromium + representative Oson HTML; AI responses mocked, no paid API call."""
import asyncio
from playwright.async_api import async_playwright
from dori.adapters import BrowserCrawler, VerificationRequired, AccessBlocked
from dori.browser_admin import BrowserAdmin, Session
from dori import ai

HTML='''<!doctype html><meta charset="utf-8"><main><h1>Результаты поиска</h1><div class="card"><h3>Авантовир таб. п/о 25мг №30 (Avantika Medex Pvt. Ltd. (Индия))</h3><p>от 243 180 сум</p><a href="/medicine/1">Подробнее</a></div></main>'''

async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        context=await browser.new_context()
        async def fixture(route):await route.fulfill(body=HTML,content_type='text/html; charset=utf-8')
        await context.route('https://osonapteka.uz/**',fixture)
        crawler=BrowserCrawler()
        crawler.context=context
        async def allowed(url):return 0
        crawler.allowed=allowed
        page=await context.new_page()
        hub=BrowserAdmin()
        hub.sessions['oson']=Session(crawler,page)
        product={'name':'АВАНТОВИР ТАБ 25МГ №30'}
        # An accessible page with no JSON-LD still passes using actual paired cards.
        result=await hub.check('oson',product,use_ai=False)
        assert result['ok'] and result['method']=='visible_cards'
        assert result['preview'][0]['price']==243180
        assert 'q=' in page.url
        # AI reads this same visible page and screenshot, rather than prompting a human.
        state='prices_visible'
        async def assess(text,url,site,screenshot=None):
            assert screenshot and url.startswith('https://osonapteka.uz/search?')
            offers=[{'name':'Авантовир таб. п/о 25мг №30','manufacturer':'Avantika Medex Pvt. Ltd.',
                     'price':243180,'currency':'UZS','site':'oson','site_id':'','url':url,
                     'extraction':'ai_requires_review'}] if state=='prices_visible' else []
            return {'state':state,'reason':'Fixture assessment','offers':offers,'proposed':len(offers),'validated':len(offers)}
        ai.analyze_visible_page=assess
        result=await hub.check('oson',product,use_ai=True)
        assert result['ok'] and result['method']=='openai'
        assert hub.sessions['oson'].status=='connected'
        original=ai.analyze_visible_page
        async def broken(*args,**kwargs):raise ai.AIExtractionError('OpenAI HTTP 401: check API key')
        ai.analyze_visible_page=broken
        result=await hub.check('oson',product,use_ai=True)
        assert result['ok'] and result['method']=='visible_cards' and '401' in result['warning']
        ai.analyze_visible_page=original
        state='human_verification'
        result=await hub.check('oson',product,use_ai=True)
        assert result['human_required'] and result['state']=='needs_attention'
        state='access_blocked'
        result=await hub.check('oson',product,use_ai=True)
        assert not result['human_required'] and result['state']=='access_blocked'
        await browser.close()
        print('Oson Chromium fixtures: visible cards 243180 UZS, AI price extraction, actual-human-only pause and hard-deny classification passed')

asyncio.run(main())
