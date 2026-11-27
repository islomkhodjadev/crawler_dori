"""Tests for the Chrome extension agent bus."""
import asyncio
import pytest
from dori.agent import AgentBus


def test_agent_bus_starts_with_no_connections():
    bus = AgentBus()
    assert bus.connected() == 0


@pytest.mark.anyio
async def test_agent_fetch_returns_none_when_no_agent_connected():
    bus = AgentBus()
    result = await bus.fetch('https://osonapteka.uz/search?q=test', 'oson')
    assert result is None


@pytest.mark.anyio
async def test_agent_fetch_returns_html_from_connected_agent():
    """Simulate an agent responding to a fetch task."""
    bus = AgentBus()
    html = '<html><body>price 10 000 сум</body></html>'

    class FakeWS:
        def __init__(self):
            self._sent = []
            self._responses = asyncio.Queue()

        async def send_json(self, data):
            self._sent.append(data)
            # Simulate agent responding with the HTML
            await self._responses.put({'id': data['id'], 'html': html, 'url': data['url'], 'status': 200})

        async def iter_json(self):
            while True:
                msg = await self._responses.get()
                yield msg

    ws = FakeWS()
    attach_task = asyncio.create_task(bus.attach(ws))
    await asyncio.sleep(0)  # let attach start

    result = await bus.fetch('https://osonapteka.uz/search?q=test', 'oson')
    attach_task.cancel()
    await asyncio.gather(attach_task, return_exceptions=True)

    assert result is not None
    assert result['html'] == html
    assert result['status'] == 200


@pytest.mark.anyio
async def test_agent_fetch_returns_none_on_error_response():
    bus = AgentBus()

    class FakeWS:
        def __init__(self):
            self._responses = asyncio.Queue()

        async def send_json(self, data):
            await self._responses.put({'id': data['id'], 'error': 'Tab load timeout'})

        async def iter_json(self):
            while True:
                yield await self._responses.get()

    ws = FakeWS()
    attach_task = asyncio.create_task(bus.attach(ws))
    await asyncio.sleep(0)

    result = await bus.fetch('https://osonapteka.uz/search?q=test', 'oson')
    attach_task.cancel()
    await asyncio.gather(attach_task, return_exceptions=True)

    assert result is None


@pytest.mark.anyio
async def test_queue_restart_with_two_connected_workers(tmp_path, monkeypatch):
    from dori.adapters import now
    from dori.agent import AgentInfo
    from dori.queue import CrawlQueue
    from dori.storage import Store

    store = Store(str(tmp_path / 'queue.sqlite3'))
    ids = store.import_products([{'name': 'АЗИТРОМИЦИН'}, {'name': 'Амоксициллин'}], 'test')
    assert store.products(search='азитромицин')['total'] == 1
    assert store.products_all_ids(search='аМОКС') == [ids[1]]
    assert store.products(page=1, page_size=1)['items'][0]['id'] == ids[1]

    queue = CrawlQueue(store.path)
    bus = AgentBus()
    for i in range(2):
        bus._agents[object()] = AgentInfo(str(i), f'Agent {i}')
    entered = asyncio.Queue()
    release = asyncio.Event()

    async def search(pinned_bus, product):
        await entered.put(product['id'])
        await release.wait()
        return [], {'status': 'no_results', 'observed_at': now()}

    monkeypatch.setattr('dori.adapters.agent_search_oson', search)

    def start():
        run_id = store.create_run(len(ids), sites=['oson'], force=True)
        store.update_run(run_id, status='running')
        session = queue.create_session(run_id, 'test', len(ids))
        queue.enqueue(session, ids, ('oson',))
        bus.start_queue_workers(queue, store, run_id)
        return run_id

    first = start()
    for _ in ids:
        await asyncio.wait_for(entered.get(), timeout=2)
    queue.stop_session()
    bus.stop_queue_workers()
    store.update_run(first, status='interrupted')
    assert queue.status()['status'] == 'stopped'
    second = start()
    workers = list(bus._worker_tasks.values())
    assert entered.empty()
    release.set()
    await asyncio.wait_for(asyncio.gather(*workers), timeout=3)
    assert not store.report(first)['products']
    assert len(store.report(second)['products']) == len(ids)
    assert queue.status()['done'] == len(ids)
    assert store.runs()[0]['status'] == 'completed'
    assert not bus._worker_tasks


@pytest.mark.anyio
async def test_parallel_collection_counts_all_completed_products(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from dori.agent import AgentInfo
    from dori.service import crawl
    from dori.storage import Store

    store = Store(str(tmp_path / 'collection.sqlite3'))
    ids = store.import_products([{'name': f'Product {i}'} for i in range(4)], 'test')
    products = store.products(ids)
    run_id = store.create_run(products, sites=['arzon', 'oson'])
    bus = AgentBus()
    for i in range(3):
        bus._agents[object()] = AgentInfo(str(i), f'Agent {i}')

    async def search(*args, **kwargs):
        await asyncio.sleep(0)
        return [], {'status': 'no_results'}

    monkeypatch.setattr('dori.adapters.agent_search_arzon', search)
    monkeypatch.setattr('dori.adapters.agent_search_oson', search)

    await crawl(store, run_id, products, admin=SimpleNamespace(collecting=False), agent_bus=bus)

    stats = store.statistics(run_id)
    assert stats['run']['status'] == 'completed'
    assert stats['products']['completed'] == 4
    assert stats['progress_percent'] == 100
    assert [source['checks_completed'] for source in stats['sources']] == [4, 4]
