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
