import asyncio
import json
from .adapters import BrowserCrawler, VerificationRequired, now
from .logs import get

log = get('service')


async def crawl(store, run_id, products, sites=('arzon', 'oson'), admin=None, use_ai=None, agent_bus=None):
    log.info('crawl run=%s start products=%d sites=%s ai=%s', run_id, len(products), list(sites), use_ai)
    store.update_run(run_id, status='running', sites=json.dumps(list(sites)), phase='Starting browsers')

    completed_products = 0

    async def collect(browser, chunk=None, bus=None):
        nonlocal completed_products
        effective_products = chunk if chunk is not None else products
        effective_bus = bus if bus is not None else agent_bus
        for product in effective_products:
            for site in sites:
                store.update_run(run_id, active_product_id=product['id'], active_site=site, phase='Searching and extracting prices')
                while True:
                    try:
                        if admin and site == 'oson' and effective_bus and effective_bus.connected():
                            from .adapters import agent_search_oson
                            from .geocode import attach_nearest_branches
                            offers, details = await agent_search_oson(effective_bus, product, use_ai=use_ai)
                            if offers:
                                await attach_nearest_branches(offers, store.stores())
                        elif admin and site == 'arzon' and effective_bus and effective_bus.connected():
                            import os
                            from .adapters import agent_search_arzon
                            from .geocode import attach_nearest_branches
                            region_id = int(os.getenv('ARZON_REGION_ID', '2114'))
                            offers, details = await agent_search_arzon(effective_bus, product, region_id=region_id)
                            if offers:
                                await attach_nearest_branches(offers, store.stores())
                        elif admin:
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
            completed_products += 1
            store.update_run(run_id, completed=completed_products, phase='Product completed')

    try:
        if admin:
            admin.collecting = True
            n_agents = agent_bus.connected() if agent_bus else 0
            if n_agents > 1:
                # Distribute products across all connected agents in parallel.
                from .agent import PinnedAgentBus
                agent_wss = list(agent_bus._agents.keys())
                n = len(agent_wss)
                chunks = [products[i::n] for i in range(n)]
                pinned = [PinnedAgentBus(agent_bus, ws) for ws in agent_wss]
                log.info('crawl run=%s distributing %d products across %d agents', run_id, len(products), n)
                await asyncio.gather(*[
                    collect(None, chunk=chunk, bus=bus)
                    for chunk, bus in zip(chunks, pinned)
                    if chunk  # skip empty chunks when agents > products
                ])
            else:
                await collect(None)
        else:
            async with BrowserCrawler(agent_bus=agent_bus) as browser:
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
