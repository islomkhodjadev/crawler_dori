"""Local Chromium fixtures only; no real WAF bypass claim or paid API calls."""
import asyncio
from playwright.async_api import async_playwright
from dori.adapters import BrowserCrawler, AccessBlocked, VerificationRequired, AutomaticVerificationTimeout, CrawlPermissionError

async def main():
 async with async_playwright() as p:
  browser=await p.chromium.launch()
  context=await browser.new_context()
  crawler=BrowserCrawler();crawler.context=context;crawler.delay=0;crawler.verification_timeout=2
  requests=[]
  mode='automatic'
  async def fixture(route):
   path=route.request.url.split('https://osonapteka.uz',1)[1]
   cookie=route.request.headers.get('cookie','')
   requests.append((path,cookie))
   if path=='/robots.txt':
    # A browser challenge sets its own cookie then reloads the protected URL.
    if 'fixture_verified=yes' not in cookie:
     await route.fulfill(status=468,content_type='text/html; charset=utf-8',body='''<p>Checking browser</p><script>setTimeout(()=>{document.cookie='fixture_verified=yes; path=/';location.reload()},80)</script>''')
    else:await route.fulfill(content_type='text/plain',body='User-agent: *\nDisallow: /private\n')
   elif mode=='automatic' and 'fixture_verified=yes' not in cookie:
    await route.fulfill(status=468,content_type='text/html',body='''<p>Checking browser</p><script>setTimeout(()=>{document.cookie='fixture_verified=yes; path=/';location.reload()},80)</script>''')
   elif mode=='denied':await route.fulfill(status=403,body='Access Forbidden')
   elif mode=='human':await route.fulfill(body='Verify you are human')
   elif mode=='stuck':await route.fulfill(status=468,body='Checking browser')
   else:await route.fulfill(content_type='text/html; charset=utf-8',body='<p>Example 25 мг №30 от 100 сум</p>')
  await context.route('https://osonapteka.uz/**',fixture)
  page=await context.new_page()
  await crawler.navigate(page,'https://osonapteka.uz/search?q=example')
  assert crawler.verification['state']=='ready'
  assert any(path.startswith('/search') and 'fixture_verified=yes' in cookie for path,cookie in requests)
  assert crawler.robots['https://osonapteka.uz'].can_fetch('DoriPriceBot','https://osonapteka.uz/search')
  try:await crawler.navigate(page,'https://osonapteka.uz/private')
  except CrawlPermissionError:pass
  else:raise AssertionError('Disallow was ignored')
  for mode,error in [('denied',AccessBlocked),('human',VerificationRequired),('stuck',AutomaticVerificationTimeout)]:
   crawler.verification_timeout=.15
   try:await crawler.navigate(page,'https://osonapteka.uz/search?q='+mode)
   except error:pass
   else:raise AssertionError(mode+' was not classified correctly')
  await browser.close()
  print('Chromium: automatic 468, shared-cookie robots verification, allowed/disallowed paths, human handoff, hard deny and timeout passed')
asyncio.run(main())
