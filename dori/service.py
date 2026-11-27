import asyncio
import json
from .adapters import BrowserCrawler, VerificationRequired, now
from .logs import get

log = get('service')


async def crawl(store, run_id, products, sites=('arzon', 'oson'), admin=None, use_ai=None, agent_bus=None,
                branch=None):
    log.info('crawl run=%s start products=%d sites=%s ai=%s', run_id, len(products), list(sites), use_ai)
    store.update_run(run_id, status='running', sites=json.dumps(list(sites)), phase='Starting browsers')

    completed_products = 0
    known_locations = store.competitor_locations() if branch else None

    async def collect(browser, chunk=None, bus=None, area=None):
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
                            if offers and not area:
                                await attach_nearest_branches(offers, store.stores())
                        elif admin and site == 'arzon' and effective_bus and effective_bus.connected():
                            import os
                            from .adapters import agent_search_arzon
                            from .geocode import attach_nearest_branches
                            region_id = int(os.getenv('ARZON_REGION_ID', '2114'))
                            offers, details = await agent_search_arzon(effective_bus, product, region_id=region_id)
                            if offers and not area:
                                await attach_nearest_branches(offers, store.stores())
                        elif area:
                            offers, details = [], {'status': 'error', 'error': 'Branch agent disconnected', 'observed_at': now()}
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
                if area:
                    from .local_area import mark_local_offers
                    center, agent_name = area
                    mark_local_offers(offers, center, branch['radius_km'], agent_name, known_locations)
                    details.update(search_lat=center[0], search_lng=center[1], search_agent=agent_name,
                                   radius_km=branch['radius_km'])
                log.info('result run=%s product_id=%s site=%s status=%s offers=%d', run_id, product['id'], site, details.get('status'), len(offers))
                store.save_result(run_id, product['id'], site, details, offers)
            completed_products += 1
            store.update_run(run_id, completed=completed_products, phase='Product completed')

    try:
        if branch:
            from .agent import PinnedAgentBus
            from .local_area import coordinates

            store.update_run(run_id, status='queued', phase='Waiting for branch agents')
            while True:
                assignments = store.agent_assignments()
                workers = []
                seen = set()
                for ws, info in reversed(list(agent_bus._agents.items())):
                    assignment = assignments.get(info.id)
                    if not assignment or assignment['store_id'] != branch['store_id'] or info.id in seen:
                        continue
                    center = coordinates(assignment['lat'], assignment['lng']) or coordinates(branch['lat'], branch['lng'])
                    if center:
                        workers.append((PinnedAgentBus(agent_bus, ws), (center, info.name)))
                        seen.add(info.id)
                if workers:
                    break
                await asyncio.sleep(2)
            store.update_run(run_id, status='running', phase='Collecting branch prices')
            await asyncio.gather(*[
                collect(None, products[index::len(workers)], bus, area)
                for index, (bus, area) in enumerate(workers[:len(products)])
            ])
        elif admin:
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
        if admin and not branch:
            admin.collecting = False
