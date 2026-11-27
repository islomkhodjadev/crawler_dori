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
  wss://predictor.03dorixona.uz/api/agent/ws?password=<password>&agent_id=<uuid>&agent_name=<name>
"""
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from .logs import get

log = get('agent')

FETCH_TIMEOUT = int(30)


@dataclass
class AgentInfo:
    id: str
    name: str
    connected_at: float = field(default_factory=time.time)
    task_count: int = 0
    last_task_at: Optional[float] = None


class AgentBus:
    """In-process broker between the crawl loop and connected Chrome extensions."""

    def __init__(self):
        self._agents: dict = {}                # ws -> AgentInfo
        self._pending: dict[str, tuple] = {}   # task_id -> (Future, ws)
        self._rr_index: int = 0
        self._queue = None                     # CrawlQueue when session active
        self._store = None                     # Store reference for queue workers
        self._run_id: Optional[int] = None     # Active queue run id
        self._worker_tasks: dict = {}          # ws -> asyncio.Task

    def connected(self) -> int:
        return len(self._agents)

    def agent_list(self) -> list[dict]:
        return [
            {
                'id': info.id,
                'name': info.name,
                'connected_at': info.connected_at,
                'task_count': info.task_count,
                'last_task_at': info.last_task_at,
                'online': True,
            }
            for info in self._agents.values()
        ]

    def _next_ws(self):
        """Round-robin across connected agents."""
        if not self._agents:
            return None
        agents = list(self._agents)
        ws = agents[self._rr_index % len(agents)]
        self._rr_index += 1
        return ws

    def _start_queue_worker(self, ws, info):
        from .queue import queue_worker

        queue, store, run_id = self._queue, self._store, self._run_id
        session_id = queue.active_session_id
        previous = self._worker_tasks.get(ws)

        async def work():
            if previous and not previous.done():
                await asyncio.gather(previous, return_exceptions=True)
            await queue_worker(PinnedAgentBus(self, ws), info.id, queue, store, run_id, session_id)

        task = asyncio.create_task(work())
        self._worker_tasks[ws] = task

        def forget(finished):
            if self._worker_tasks.get(ws) is finished:
                del self._worker_tasks[ws]
            if not finished.cancelled() and (error := finished.exception()):
                log.error('queue worker failed agent=%s run=%s', info.id, run_id, exc_info=error)

        task.add_done_callback(forget)

    def start_queue_workers(self, queue, store, run_id: int) -> None:
        """Start workers for the new session on every connected socket."""
        self._queue = queue
        self._store = store
        self._run_id = run_id
        for ws, info in self._agents.items():
            self._start_queue_worker(ws, info)

    def stop_queue_workers(self) -> None:
        """Clear queue refs so new workers are not started; running workers exit naturally."""
        self._queue = None
        self._store = None
        self._run_id = None

    async def attach(self, ws, agent_id: str = '', agent_name: str = '') -> None:
        info = AgentInfo(
            id=agent_id or str(uuid.uuid4()),
            name=agent_name or 'Агент',
        )
        self._agents[ws] = info
        log.info('agent connected id=%s name=%r total=%d', info.id, info.name, len(self._agents))

        if self._queue and self._queue.is_running:
            self._start_queue_worker(ws, info)

        try:
            async for message in ws.iter_json():
                if not isinstance(message, dict):
                    continue
                if message.get('type') == 'ping':
                    continue
                task_id = message.get('id')
                if task_id and task_id in self._pending:
                    fut, _ = self._pending.pop(task_id)
                    if not fut.done():
                        fut.set_result(message)
                        log.info('agent task id=%s status=%s', task_id,
                                 message.get('status', message.get('error', '?')))
        except Exception as exc:
            log.info('agent disconnected id=%s: %s', info.id, type(exc).__name__)
        finally:
            # Cancel this agent's queue worker
            task = self._worker_tasks.pop(ws, None)
            if task and not task.done():
                task.cancel()
            self._agents.pop(ws, None)
            # Cancel pending futures dispatched to this ws so callers fail fast
            stale = [tid for tid, (fut, w) in list(self._pending.items()) if w is ws]
            for tid in stale:
                entry = self._pending.pop(tid, None)
                if entry:
                    fut, _ = entry
                    if not fut.done():
                        fut.cancel()
                        log.info('agent cancelled stale task id=%s (agent disconnected)', tid)
            log.info('agent removed id=%s total=%d', info.id, len(self._agents))

    async def _send_task(self, payload: dict, timeout: int = FETCH_TIMEOUT,
                         ws=None) -> Optional[dict]:
        """Low-level: send a task to an extension and await its response."""
        if not self._agents:
            return None
        if ws is None:
            ws = self._next_ws()
        if ws not in self._agents:
            return None
        info = self._agents[ws]
        task_id = str(uuid.uuid4())
        payload = {**payload, 'id': task_id}
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[task_id] = (fut, ws)
        info.task_count += 1
        info.last_task_at = time.time()
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
        except asyncio.CancelledError:
            self._pending.pop(task_id, None)
            raise
        except Exception as exc:
            log.warning('agent task failed id=%s: %s: %s', task_id, type(exc).__name__, exc)
            self._pending.pop(task_id, None)
            return None

    async def fetch(self, url: str, site: str, ws=None) -> Optional[dict]:
        """Ask a connected extension to fetch url. Returns {html, url, status} or None."""
        log.info('agent fetch site=%s url=%.120s', site, url)
        return await self._send_task({'type': 'fetch', 'url': url, 'site': site}, ws=ws)

    async def fetch_arzon_pharmacies(self, product_url: str, region_id: int,
                                     ws=None) -> Optional[dict]:
        """Ask extension to: add product to cart, load pharmacy chooser, return HTML, clear cart."""
        log.info('agent arzon_pharmacies product_url=%.120s region_id=%s', product_url, region_id)
        return await self._send_task(
            {'type': 'arzon_pharmacies', 'product_url': product_url, 'region_id': region_id},
            timeout=90, ws=ws,
        )

    async def fetch_oson_pharmacies(self, product_url: str, region_ids: list,
                                    ws=None) -> Optional[dict]:
        """Ask extension to: set Oson region, fetch product page, return SSR HTML with per-pharmacy prices."""
        log.info('agent oson_pharmacies product_url=%.120s region_ids=%s', product_url, region_ids)
        return await self._send_task(
            {'type': 'oson_pharmacies', 'product_url': product_url, 'region_ids': region_ids},
            timeout=60, ws=ws,
        )


class PinnedAgentBus:
    """Thin wrapper that routes all tasks to one specific connected ws.

    Used by the parallel crawl loop so each product-chunk stays on one agent.
    """

    def __init__(self, bus: AgentBus, ws):
        self._bus = bus
        self._ws = ws

    def connected(self) -> int:
        return 1 if self._ws in self._bus._agents else 0

    async def fetch(self, url: str, site: str) -> Optional[dict]:
        return await self._bus.fetch(url, site, ws=self._ws)

    async def fetch_arzon_pharmacies(self, product_url: str, region_id: int) -> Optional[dict]:
        return await self._bus.fetch_arzon_pharmacies(product_url, region_id, ws=self._ws)

    async def fetch_oson_pharmacies(self, product_url: str, region_ids: list) -> Optional[dict]:
        return await self._bus.fetch_oson_pharmacies(product_url, region_ids, ws=self._ws)
