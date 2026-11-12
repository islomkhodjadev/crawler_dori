import asyncio
import json
from .adapters import BrowserCrawler, VerificationRequired, now
from .logs import get

log = get('service')


async def crawl(store, run_id, products, sites=('arzon', 'oson'), admin=None, use_ai=None):
    log.info('crawl run=%s start products=%d sites=%s ai=%s', run_id, len(products), list(sites), use_ai)
    store.update_run(run_id, status='running', sites=json.dumps(list(sites)), phase='Starting browsers')
    async def collect(browser):
        for i, product in enumerate(products):
            for site in sites:
                store.update_run(run_id, active_product_id=product['id'], active_site=site, phase='Searching and extracting prices')
                while True:
                    try:
                        if admin:
                            offers, details = await admin.search(site, product, use_ai=use_ai)
                        else:
                            offers, details = await browser.search(site, product, use_ai=use_ai)
                        break
                    except VerificationRequired as exc:
                        if admin:
                            log.warning('run=%s product_id=%s site=%s paused for verification: %s', run_id, product['id'], site, exc)
                            store.update_run(run_id, status='waiting_for_human', phase='Waiting for website verification', error=f'{site}: {str(exc)[:500]}')
                            await admin.wait_for_human(site, str(exc))
                            store.update_run(run_id, status='running', phase='Retrying after verification', error=None)
                            continue
                        offers, details = [], {'status':'error','error':str(exc), 'observed_at':now()}
                        break
                    except Exception as exc:
                        offers, details = [], {'status':'error','error':f'{type(exc).__name__}: {str(exc)[:700]}', 'observed_at':now()}
                        break
                log.info('result run=%s product_id=%s site=%s status=%s offers=%d', run_id, product['id'], site, details.get('status'), len(offers))
                store.save_result(run_id, product['id'], site, details, offers)
            store.update_run(run_id, completed=i + 1, phase='Product completed')
    try:
        if admin:
            admin.collecting = True
            await collect(None)
        else:
            async with BrowserCrawler() as browser:
                await collect(browser)
        log.info('crawl run=%s completed', run_id)
        store.update_run(run_id, status='completed', finished_at=now(), error=None, active_product_id=None, active_site=None, phase='Completed')
    except asyncio.CancelledError:
        log.warning('crawl run=%s cancelled', run_id)
        store.update_run(run_id, status='interrupted', finished_at=now(), error='Crawl cancelled', active_product_id=None, active_site=None, phase='Cancelled')
        raise
    except Exception as exc:
        log.exception('crawl run=%s failed', run_id)
        store.update_run(run_id, status='failed', finished_at=now(), active_product_id=None, active_site=None, phase='Failed', error=f'{type(exc).__name__}: {str(exc)[:700]}')
    finally:
        if admin:
            admin.collecting = False
