"""Chrome extension agent: browser tasks dispatched to a user's real Chrome via WebSocket.

The server cannot fetch Oson directly (IP blocked by SafeLine WAF). This module
lets the crawl loop delegate fetches to a connected Chrome extension running in
the user's own browser. Their IP, their cookies, no Playwright/CDP involved.

Protocol (all JSON over WebSocket /api/agent/ws):
  server → extension: {"id": "<uuid>", "type": "fetch", "url": "<url>", "site": "oson"}
  extension → server: {"id": "<uuid>", "html": "<page html>", "url": "<final url>", "status": 200}
  extension → server: {"id": "<uuid>", "error": "<message>"}
  extension → server: {"type": "ping"}   (keepalive, no id)

The extension must present the admin password in the WebSocket query string:
  wss://predictor.03dorixona.uz/api/agent/ws?password=<password>
"""
import asyncio
import uuid
from typing import Optional
from .logs import get

log = get('agent')

# How long to wait for the extension to return a page (seconds)
FETCH_TIMEOUT = int(30)


class AgentBus:
    """In-process broker between the crawl loop and connected Chrome extensions."""

    def __init__(self):
        self._websockets: list = []          # connected extension WebSockets
        self._pending: dict[str, asyncio.Future] = {}

    def connected(self) -> int:
        return len(self._websockets)

    async def attach(self, ws) -> None:
        self._websockets.append(ws)
        log.info('agent connected total=%d', len(self._websockets))
        try:
            async for message in ws.iter_json():
                if not isinstance(message, dict):
                    continue
                if message.get('type') == 'ping':
                    continue
                task_id = message.get('id')
                if task_id and task_id in self._pending:
                    fut = self._pending.pop(task_id)
                    if not fut.done():
                        fut.set_result(message)
                        log.info('agent task id=%s status=%s', task_id,
                                 message.get('status', message.get('error', '?')))
        except Exception as exc:
            log.info('agent disconnected: %s', type(exc).__name__)
        finally:
            self._websockets.remove(ws)
            log.info('agent disconnected total=%d', len(self._websockets))

    async def _send_task(self, payload: dict, timeout: int = FETCH_TIMEOUT) -> Optional[dict]:
        """Low-level: send a task to the extension and await its response."""
        if not self._websockets:
            return None
        ws = self._websockets[-1]
        task_id = str(uuid.uuid4())
        payload = {**payload, 'id': task_id}
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[task_id] = fut
        try:
            await ws.send_json(payload)
            result = await asyncio.wait_for(fut, timeout=timeout)
            if 'error' in result:
                log.warning('agent task error id=%s: %s', task_id, result['error'])
                return None
            return result
        except asyncio.TimeoutError:
            log.warning('agent task timeout id=%s type=%s', task_id, payload.get('type'))
            self._pending.pop(task_id, None)
            return None
        except Exception as exc:
            log.warning('agent task failed id=%s: %s: %s', task_id, type(exc).__name__, exc)
            self._pending.pop(task_id, None)
            return None

    async def fetch(self, url: str, site: str) -> Optional[dict]:
        """Ask a connected extension to fetch url. Returns {html, url, status} or None."""
        log.info('agent fetch site=%s url=%.120s', site, url)
        return await self._send_task({'type': 'fetch', 'url': url, 'site': site})

    async def fetch_arzon_pharmacies(self, product_url: str, region_id: int) -> Optional[dict]:
        """Ask extension to: add product to cart, load pharmacy chooser, return HTML, clear cart."""
        log.info('agent arzon_pharmacies product_url=%.120s region_id=%s', product_url, region_id)
        return await self._send_task(
            {'type': 'arzon_pharmacies', 'product_url': product_url, 'region_id': region_id},
            timeout=90,
        )

    async def fetch_oson_pharmacies(self, product_url: str, region_ids: list) -> Optional[dict]:
        """Ask extension to: set Oson region, fetch product page, return SSR HTML with per-pharmacy prices."""
        log.info('agent oson_pharmacies product_url=%.120s region_ids=%s', product_url, region_ids)
        return await self._send_task(
            {'type': 'oson_pharmacies', 'product_url': product_url, 'region_ids': region_ids},
            timeout=60,
        )
