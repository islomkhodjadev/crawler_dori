"""Optional real-Chromium interaction check: PYTHONPATH=. python tests/browser_smoke.py."""
import asyncio
import base64
from playwright.async_api import async_playwright
from dori.browser_admin import BrowserAdmin, Session
from dori.adapters import BrowserCrawler

async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        context=await browser.new_context(viewport={'width':1280,'height':800})
        page=await context.new_page()
        await page.set_content('''<button style="position:absolute;left:20px;top:20px;width:150px;height:40px" onclick="this.textContent='Allowed'">Allow</button><input id="query" style="position:absolute;left:20px;top:100px;width:250px;height:40px">''')
        hub=BrowserAdmin()
        hub.sessions['arzon']=Session(None,page)
        await hub.input('arzon',{'type':'click','x':60,'y':40})
        assert await page.locator('button').inner_text()=='Allowed'
        await hub.input('arzon',{'type':'click','x':80,'y':120})
        await hub.input('arzon',{'type':'text','text':'Medicine №30'})
        assert await page.locator('#query').input_value()=='Medicine №30'
        await hub.input('arzon',{'type':'key','key':'Control+A'})
        await hub.input('arzon',{'type':'text','text':'Changed'})
        assert await page.locator('#query').input_value()=='Changed'
        class Socket:
            got_frame=asyncio.Event()
            frame=None
            async def accept(self):pass
            async def send_json(self,data):
                if data['type']=='frame':
                    self.frame=data
                    self.got_frame.set()
            async def receive_json(self):await asyncio.Event().wait()
        socket=Socket()
        task=asyncio.create_task(hub.stream(socket,'arzon'))
        await asyncio.wait_for(socket.got_frame.wait(),5)
        assert base64.b64decode(socket.frame['image']).startswith(b'\xff\xd8')
        task.cancel()
        try:await task
        except asyncio.CancelledError:pass
        assert not hub.sessions['arzon'].viewer
        # Regression: a hydrated card may exist before its price and link arrive.
        markup = """<article data-product-card='detailed'></article><script>setTimeout(()=>{document.querySelector('article').innerHTML='<p>Example 20 mg tab №30</p><span>Example Pharma</span><p>от 1 200 сўм</p><a aria-label=\"Example 20 mg tab №30\" href=\"/ru/products/example-1\"></a>';},500)</script>"""
        async def fixture(route):await route.fulfill(body=markup,content_type='text/html; charset=utf-8')
        await context.route('https://arzonapteka.uz/**',fixture)
        crawler=BrowserCrawler()
        crawler.context=context
        async def allowed(url):return 0
        crawler.allowed=allowed
        offers, details=await crawler.search('arzon',{'name':'Example 20 mg tab №30'})
        assert details['status']=='ok' and offers[0]['price']==1200
        await browser.close()
        print('Real Chromium: remote Allow click, text/key input, JPEG stream and viewer cleanup passed')

asyncio.run(main())
