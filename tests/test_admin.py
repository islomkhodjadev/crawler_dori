import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dori.app import create_app
from dori.adapters import VerificationRequired
from dori.browser_admin import BrowserAdmin, Session
from dori.service import crawl
from dori.storage import Store


def item():
    return {'name':'Example 20 mg tab №30','manufacturer':'Example Pharma','invoice_price':100}


def test_admin_password_and_websocket_origin(tmp_path, monkeypatch):
    monkeypatch.setenv('DORI_ADMIN_PASSWORD','example-test-password')
    with TestClient(create_app(str(tmp_path/'db.sqlite3'))) as client:
        assert client.get('/api/products').status_code == 401
        assert client.get('/').url.path == '/login'
        assert client.post('/api/login',data={'password':'wrong'}).status_code == 401
        response=client.post('/api/login',data={'password':'example-test-password'},follow_redirects=False)
        assert response.status_code == 303
        assert 'HttpOnly' in response.headers['set-cookie']
        assert client.get('/api/browsers').status_code == 200
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect('/api/browsers/arzon/stream',headers={'Origin':'https://evil.example'}):
                pass
        assert denied.value.code == 1008
        assert client.post('/api/logout').json()['ok']
        assert client.get('/api/products').status_code == 401


def test_crawl_requires_checked_connections(tmp_path):
    with TestClient(create_app(str(tmp_path/'db.sqlite3'))) as client:
        pid=client.post('/api/products',json={'name':'Example'}).json()['id']
        response=client.post('/api/crawl',json={'product_ids':[pid]})
        assert response.status_code == 409
        assert 'check' in response.json()['detail']
        assert client.post('/api/browsers/unknown/open',json={}).status_code == 400
        assert client.post('/api/browsers/arzon/check',json={'product_id':999}).status_code == 404
        assert client.post('/api/browsers/arzon/check',json={'product_id':pid}).status_code == 409


def test_connection_check_demands_real_prices():
    async def run():
        hub=BrowserAdmin()
        async def search(*args,**kwargs):
            return [], {'status':'no_results'}
        crawler=SimpleNamespace(robots={},search=search)
        s=Session(crawler,SimpleNamespace(url='https://arzonapteka.uz/ru'))
        hub.sessions['arzon']=s
        result=await hub.check('arzon',item())
        assert not result['ok'] and s.status=='needs_attention'
        assert not s.ready.is_set()
        async def prices(*args,**kwargs):
            return [{'price':123}], {'status':'ok'}
        crawler.search=prices
        result=await hub.check('arzon',item())
        assert result['ok'] and s.ready.is_set()
        hub.require_ready(['arzon'])
        s.checked_monotonic=time.monotonic()-901
        with pytest.raises(ValueError):hub.require_ready(['arzon'])
    asyncio.run(run())


def test_human_input_locked_during_collection():
    async def run():
        hub=BrowserAdmin()
        hub.sessions['arzon']=Session(None,None,status='connected')
        hub.collecting=True
        with pytest.raises(ValueError):
            await hub.input('arzon',{'type':'click','x':1,'y':1})
        hub.collecting=False
        with pytest.raises(ValueError):
            await hub.input('arzon',{'type':'click','x':9000,'y':1})
        with pytest.raises(ValueError):
            await hub.input('arzon',{'type':'evaluate','code':'unsafe'})
    asyncio.run(run())


def test_crawl_pauses_and_resumes_same_product(tmp_path):
    async def run():
        store=Store(str(tmp_path/'db.sqlite3'))
        store.import_products([item()], 'test')
        products=store.products()
        run_id=store.create_run(products)
        class Hub:
            collecting=False
            calls=0
            ready=asyncio.Event()
            async def search(self,*args,**kwargs):
                self.calls+=1
                if self.calls==1:raise VerificationRequired('verify')
                return [],{'status':'no_results'}
            async def wait_for_human(self,*args):
                await self.ready.wait()
        hub=Hub()
        task=asyncio.create_task(crawl(store,run_id,products,('arzon',),admin=hub))
        await asyncio.sleep(.02)
        assert store.runs()[0]['status']=='waiting_for_human'
        with pytest.raises(ValueError):store.create_run(products)
        assert not store.report(run_id)['products']
        hub.ready.set()
        await task
        assert hub.calls==2
        assert store.runs()[0]['status']=='completed'
        assert store.runs()[0]['completed']==1
        assert not hub.collecting
    asyncio.run(run())


def test_cancel_during_human_pause(tmp_path):
    async def run():
        store=Store(str(tmp_path/'db.sqlite3'))
        store.import_products([item()], 'test')
        run_id=store.create_run(store.products())
        class Hub:
            collecting=False
            async def search(self,*args,**kwargs):raise VerificationRequired('verify')
            async def wait_for_human(self,*args):await asyncio.Event().wait()
        hub=Hub()
        task=asyncio.create_task(crawl(store,run_id,store.products(),('arzon',),admin=hub))
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert store.runs()[0]['status']=='interrupted'
        assert not hub.collecting
    asyncio.run(run())


def test_server_recovery_includes_human_pauses(tmp_path):
    store=Store(str(tmp_path/'db.sqlite3'))
    rid=store.create_run([item()])
    store.update_run(rid,status='waiting_for_human')
    store.recover()
    assert store.runs()[0]['status']=='interrupted'
