import asyncio
from .adapters import BrowserCrawler, now


async def crawl(store, run_id, products, sites=('arzon', 'oson')):
    store.update_run(run_id, status='running')
    try:
        async with BrowserCrawler() as browser:
            for i, product in enumerate(products):
                for site in sites:
                    try:
                        offers, details = await browser.search(site, product)
                    except Exception as exc:
                        offers = []
                        details = {'status': 'error', 'error': f'{type(exc).__name__}: {str(exc)[:700]}', 'observed_at': now()}
                    store.save_result(run_id, product['id'], site, details, offers)
                store.update_run(run_id, completed=i + 1)
        store.update_run(run_id, status='completed', finished_at=now())
    except asyncio.CancelledError:
        store.update_run(run_id, status='interrupted', finished_at=now(), error='Crawl cancelled')
        raise
    except Exception as exc:
        store.update_run(run_id, status='failed', finished_at=now(), error=f'{type(exc).__name__}: {str(exc)[:700]}')
