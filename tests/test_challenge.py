import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from dori.challenge import challenge_state, retry_after_seconds, settle
from dori.adapters import BrowserCrawler, RateLimited


@pytest.mark.parametrize('status,text,html,state',[
 (468,'Checking browser','<script src="challenge.js"></script>','automatic_verification'),
 (200,'Dynamic Decrypting','<script src="/.safeline/challenge.js"></script>','automatic_verification'),
 (403,'Access Forbidden','SafeLine','access_blocked'),
 (200,'Debugging Detected','SafeLine','access_blocked'),
 (200,'Verify you are human','captcha','human_verification'),
 (429,'Please wait','','rate_limited'),
 (200,'Example 25 mg №30 от 100 сум','<script src="challenge.js"></script>','prices_visible'),
 (200,'Nothing here','','unknown')])
def test_state(status,text,html,state):assert challenge_state(status,text,html)==state


def test_retry_after_supports_date_and_rejects_bad_values():
    assert retry_after_seconds('12')==12
    assert retry_after_seconds('-1')==0
    assert retry_after_seconds('nonsense') is None
    assert retry_after_seconds('inf') is None
    now=datetime(2026,11,15,12,0,0,tzinfo=timezone.utc)
    assert retry_after_seconds('Sun, 15 Nov 2026 12:01:00 GMT',now)==60


class Page:
    url='https://osonapteka.uz/search?q=example'
    main_frame=None
    def __init__(self,text,html):self.text=text;self.html=html;self.listeners={}
    def on(self,name,fn):self.listeners[name]=fn
    def remove_listener(self,name,fn):self.listeners.pop(name,None)
    def locator(self,*args):return self
    async def inner_text(self,**kwargs):return self.text
    async def content(self):return self.html


def test_automatic_challenge_timeout_does_not_claim_success():
    async def run():
        page=Page('Checking browser','<script src="challenge.js"></script>')
        result=await settle(page,SimpleNamespace(status=468),.01,poll=.005)
        assert result.state=='verification_timeout' and result.automatic
        assert not page.listeners
    asyncio.run(run())


def test_challenge_finishes_when_browser_navigates():
    async def run():
        page=Page('Checking browser','<script src="challenge.js"></script>')
        async def complete():
            await asyncio.sleep(.01)
            page.text='Example 25 mg №30 от 100 сум';page.html='<main>Prices</main>'
            page.listeners['response'](SimpleNamespace(status=200,request=SimpleNamespace(is_navigation_request=lambda:True,frame=None)))
        task=asyncio.create_task(complete())
        result=await settle(page,SimpleNamespace(status=468),.3,poll=.005)
        await task
        assert result.state=='prices_visible' and result.status==200
        assert not page.listeners
    asyncio.run(run())


def test_long_retry_after_is_not_shortened(monkeypatch):
    async def run():
        crawler=BrowserCrawler();crawler.max_backoff=.01
        calls=[]
        async def goto(*args,**kwargs):
            calls.append(args)
            return SimpleNamespace(status=429,headers={'retry-after':'120'})
        with pytest.raises(RateLimited,match='120'):
            await crawler.browser_load(SimpleNamespace(goto=goto),'https://osonapteka.uz/')
        assert len(calls)==1
        assert crawler.verification['state']=='rate_limited'
    asyncio.run(run())


def test_unresponsive_renderer_is_bounded():
    async def run():
        page=Page('Checking browser','')
        async def hangs():await asyncio.Event().wait()
        page.content=hangs
        result=await asyncio.wait_for(settle(page,SimpleNamespace(status=468),.03,poll=.005),.3)
        assert result.state=='verification_timeout'
        assert not page.listeners
    asyncio.run(run())
