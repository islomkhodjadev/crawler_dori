import asyncio
import json
from types import SimpleNamespace
import pytest
import httpx
from fastapi.testclient import TestClient

from dori.adapters import (BrowserCrawler, ExtractionError, VerificationRequired,
                           AccessBlocked, RateLimited, CrawlPermissionError)
from dori.ai import (ParsedPage, ParsedOffer, validate_visible_offers,
                     analyze_visible_page, AIExtractionError, response_schema)
from dori.oson_cards import parse_oson_cards
from dori.page_state import classify_text
from dori.browser_admin import BrowserAdmin, Session
from dori.app import create_app

OSON='''<main><h1>Результаты поиска</h1><p>Показано лекарств: 2</p>
<div class="card"><div><h3>Авантовир таб. п/о 25мг №30 (Avantika Medex (Индия), Avantika Medex Pvt. Ltd. (Индия))</h3><p><span>от 243 180 сум</span></p></div><a href="/medicine/1">Подробнее</a><a href="/prices/1">Цены в аптеках</a></div>
<div class="card"><h3>Квантавир таб. п/о 0.5мг №30 (Abdi Ibrahim (Турция))</h3><p>от 385 000 сум</p><a href="/medicine/2">Подробнее</a></div></main>'''


def test_oson_visible_cards_pair_names_and_prices():
    offers=parse_oson_cards(OSON,'https://osonapteka.uz/search?q=авантовир')
    assert len(offers)==2
    assert offers[0]['name']=='Авантовир таб. п/о 25мг №30'
    assert offers[0]['price']==243180
    assert offers[1]['price']==385000
    assert 'Авантовир' not in offers[1]['name']
    assert offers[0]['extraction']=='visible_card_requires_review'


def test_do_not_pair_neighbor_title_with_orphan_price():
    html='<main><div><h3>Другой препарат 25 мг №30</h3></div><div><p>от 243 180 сум</p></div><div><p>от 385 000 сум</p></div></main>'
    assert not parse_oson_cards(html,'https://osonapteka.uz/search?q=test')


@pytest.mark.parametrize('text,state',[
    ('Авантовир 25мг №30 от 243 180 сум. Footer: captcha security','prices_visible'),
    ('Verify you are human','human_verification'),
    ('Access Forbidden Security Detection Powered By SafeLine','access_blocked'),
    ('Too many requests','rate_limited'),('Показано лекарств: 0','no_results')])
def test_page_states(text,state):assert classify_text(text)==state


def test_ai_evidence_tolerates_label_spacing_without_inventing_price():
    parsed=ParsedPage(offers=[ParsedOffer(name='Авантовир таб. п/о 25 мг №30',manufacturer='Avantika Medex Pvt. Ltd.',price=243180,currency='UZS'),
                              ParsedOffer(name='Авантовир таб. п/о 25 мг №30',manufacturer='',price=1,currency='UZS')])
    from bs4 import BeautifulSoup
    text=BeautifulSoup(OSON,'html.parser').get_text(' ',strip=True)
    offers=validate_visible_offers(parsed,text,'https://osonapteka.uz/search?q=test','oson')
    assert len(offers)==1 and offers[0]['price']==243180


@pytest.mark.parametrize('site',['oson','arzon'])
@pytest.mark.parametrize('state,exception',[
    ('human_verification',VerificationRequired),('access_blocked',AccessBlocked),
    ('rate_limited',RateLimited),('unknown',ExtractionError)])
def test_shared_ai_routing_for_both_sites(site,state,exception):
    async def run():
        crawler=BrowserCrawler()
        async def assess(*args):return {'state':state,'reason':'Reason','offers':[],'validated':0,'proposed':0}
        crawler.assess_page=assess
        with pytest.raises(exception):await crawler.ai_extract(SimpleNamespace(url='https://example.test'),site)
    asyncio.run(run())


@pytest.mark.parametrize('site',['oson','arzon'])
def test_ai_visible_prices_do_not_request_a_person(site):
    async def run():
        crawler=BrowserCrawler()
        async def assess(*args):return {'state':'prices_visible','reason':'Loaded medicine prices','offers':[{'name':'Example','price':243180}],'validated':1,'proposed':1}
        crawler.assess_page=assess
        offers,details=await crawler.ai_extract(SimpleNamespace(url='https://example.test'),site)
        assert offers[0]['price']==243180 and details['extraction_method']=='openai'
    asyncio.run(run())


@pytest.mark.parametrize('error,state,human',[
    (ExtractionError('AI failed'),'extraction_error',False),
    (AccessBlocked('Denied'),'access_blocked',False),
    (RateLimited('Wait'),'rate_limited',False),
    (VerificationRequired('Click challenge'),'needs_attention',True)])
def test_admin_error_routing(error,state,human):
    async def run():
        async def search(*args,**kwargs):raise error
        hub=BrowserAdmin()
        hub.sessions['oson']=Session(SimpleNamespace(robots={},search=search),None)
        result=await hub.check('oson',{'name':'Example'})
        assert result['state']==state and result['human_required']==human
    asyncio.run(run())


def test_panel_defaults_ai_on_when_key_configured(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder')
    app=create_app(str(tmp_path/'db.sqlite3'))
    seen=[]
    async def check(site,product,use_ai=False):
        seen.append(use_ai)
        return {'ok':True,'listings':1}
    app.state.browsers.check=check
    with TestClient(app) as client:
        assert client.get('/api/health').json()['ai_default_enabled']
        pid=client.post('/api/products',json={'name':'Example'}).json()['id']
        assert client.post('/api/browsers/oson/check',json={'product_id':pid}).json()['ok']
        assert client.post('/api/browsers/arzon/check',json={'product_id':pid,'use_ai':False}).json()['ok']
    assert seen==[True,False]


def test_openai_assessment_request_has_vision_and_structured_schema(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder')
    captured={}
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,*args,**kwargs):
            captured.update(kwargs['json'])
            output={'state':'prices_visible','reason':'Product card loaded','offers':[{'name':'Авантовир 25мг №30','manufacturer':'','price':243180,'currency':'UZS'}]}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(output)}}]})
    monkeypatch.setattr('dori.ai.httpx.AsyncClient',Client)
    result=asyncio.run(analyze_visible_page('Авантовир 25мг №30 от 243 180 сум','https://osonapteka.uz/search?q=x','oson',b'jpeg'))
    assert result['validated']==1
    assert captured['messages'][1]['content'][1]['type']=='image_url'
    schema=captured['response_format']['json_schema']['schema']
    assert set(schema['required'])=={'offers','state','reason'}


def test_openai_error_reports_configuration_not_verification(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder')
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,*args,**kwargs):return httpx.Response(401,json={'error':{'code':'invalid_api_key','message':'sensitive detail'}})
    monkeypatch.setattr('dori.ai.httpx.AsyncClient',Client)
    with pytest.raises(AIExtractionError,match='HTTP 401') as error:
        asyncio.run(analyze_visible_page('x','https://osonapteka.uz/','oson'))
    assert 'sensitive' not in str(error.value)
