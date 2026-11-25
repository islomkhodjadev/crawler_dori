"""Pull-based crawl queue backed by SQLite — handles 20k+ products reliably.

Flow:
  Admin hits POST /api/crawl/start → session created, all product×site tasks enqueued
  AgentBus.start_queue_workers() → one worker coroutine per connected agent
  Worker loop: claim next pending task → process via PinnedAgentBus → save result → repeat
  Watchdog resets in-progress tasks that timed out (up to MAX_RETRIES)
  New agents connecting mid-session automatically get a worker started in attach()
"""
import asyncio
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from .logs import get

log = get('queue')

TASK_TIMEOUT = 90   # seconds before resetting a stuck in_progress task
MAX_RETRIES = 3


class CrawlQueue:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._active_session_id: str | None = None

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @property
    def is_running(self) -> bool:
        return self._active_session_id is not None

    def create_session(self, run_id: int, started_by: str, total: int) -> str:
        session_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute(
                'INSERT INTO crawl_sessions (id, run_id, started_by, started_at, total, status) VALUES (?,?,?,?,?,?)',
                (session_id, run_id, started_by, time.time(), total, 'running')
            )
        self._active_session_id = session_id
        log.info('queue session created id=%s run=%s total=%d', session_id, run_id, total)
        return session_id

    def enqueue(self, session_id: str, product_ids: list, sites: tuple):
        """Insert one crawl_queue row per product × site."""
        with self._connect() as db:
            for product_id in product_ids:
                for site in sites:
                    db.execute(
                        'INSERT INTO crawl_queue (id, session_id, product_id, site, status) VALUES (?,?,?,?,?)',
                        (str(uuid.uuid4()), session_id, int(product_id), site, 'pending')
                    )
        log.info('queue enqueued session=%s products=%d sites=%s tasks=%d',
                 session_id, len(product_ids), list(sites), len(product_ids) * len(sites))

    def next_task(self, agent_id: str) -> dict | None:
        """Atomically claim the next pending task. Returns row dict or None."""
        if not self._active_session_id:
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT id, product_id, site FROM crawl_queue "
                "WHERE session_id=? AND status='pending' ORDER BY rowid LIMIT 1",
                (self._active_session_id,)
            ).fetchone()
            if not row:
                return None
            db.execute(
                'UPDATE crawl_queue SET status=?, agent_id=?, started_at=? WHERE id=?',
                ('in_progress', agent_id, time.time(), row['id'])
            )
            return dict(row)

    def complete_task(self, task_id: str, error: str | None = None):
        status = 'failed' if error else 'done'
        with self._connect() as db:
            db.execute(
                'UPDATE crawl_queue SET status=?, finished_at=?, error=? WHERE id=?',
                (status, time.time(), error, task_id)
            )
            if self._active_session_id:
                col = 'failed' if error else 'done'
                db.execute(f'UPDATE crawl_sessions SET {col}={col}+1 WHERE id=?',
                           (self._active_session_id,))
        self._check_complete()

    def _check_complete(self):
        if not self._active_session_id:
            return
        with self._connect() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM crawl_queue WHERE session_id=? AND status IN ('pending','in_progress')",
                (self._active_session_id,)
            ).fetchone()[0]
            if count == 0:
                db.execute(
                    "UPDATE crawl_sessions SET status='done', finished_at=? WHERE id=?",
                    (time.time(), self._active_session_id)
                )
                log.info('queue session completed id=%s', self._active_session_id)
                self._active_session_id = None

    def stop_session(self):
        if not self._active_session_id:
            return
        sid = self._active_session_id
        with self._connect() as db:
            db.execute('UPDATE crawl_sessions SET status=?, finished_at=? WHERE id=?',
                       ('stopped', time.time(), sid))
            db.execute(
                "UPDATE crawl_queue SET status='cancelled' "
                "WHERE session_id=? AND status IN ('pending','in_progress')",
                (sid,)
            )
        self._active_session_id = None
        log.info('queue session stopped id=%s', sid)

    def status(self) -> dict:
        if not self._active_session_id:
            return {'status': 'idle', 'total': 0, 'done': 0, 'failed': 0,
                    'in_progress': 0, 'pending': 0}
        with self._connect() as db:
            session = db.execute(
                'SELECT * FROM crawl_sessions WHERE id=?', (self._active_session_id,)
            ).fetchone()
            if not session:
                return {'status': 'idle', 'total': 0, 'done': 0, 'failed': 0,
                        'in_progress': 0, 'pending': 0}
            counts = {
                row['status']: row['count']
                for row in db.execute(
                    'SELECT status, COUNT(*) as count FROM crawl_queue '
                    'WHERE session_id=? GROUP BY status',
                    (self._active_session_id,)
                ).fetchall()
            }
        return {
            'session_id': session['id'],
            'run_id': session['run_id'],
            'status': session['status'],
            'total': session['total'],
            'done': counts.get('done', 0),
            'failed': counts.get('failed', 0),
            'in_progress': counts.get('in_progress', 0),
            'pending': counts.get('pending', 0),
        }

    async def watchdog(self):
        """Background task: reset timed-out in_progress tasks every 30 s."""
        while True:
            await asyncio.sleep(30)
            if not self._active_session_id:
                continue
            cutoff = time.time() - TASK_TIMEOUT
            with self._connect() as db:
                timed_out = db.execute(
                    "SELECT id, retries FROM crawl_queue "
                    "WHERE session_id=? AND status='in_progress' AND started_at<?",
                    (self._active_session_id, cutoff)
                ).fetchall()
                for row in timed_out:
                    if row['retries'] >= MAX_RETRIES:
                        db.execute(
                            "UPDATE crawl_queue SET status='failed', error='Max retries exceeded' WHERE id=?",
                            (row['id'],)
                        )
                        db.execute('UPDATE crawl_sessions SET failed=failed+1 WHERE id=?',
                                   (self._active_session_id,))
                        log.warning('queue task %s permanently failed (max retries)', row['id'])
                    else:
                        db.execute(
                            'UPDATE crawl_queue SET status=?, retries=retries+1, '
                            'agent_id=NULL, started_at=NULL WHERE id=?',
                            ('pending', row['id'])
                        )
                        log.info('queue task %s reset to pending retry %d', row['id'], row['retries'] + 1)
            self._check_complete()


async def queue_worker(pinned_bus, agent_id: str, queue: CrawlQueue, store, run_id: int):
    """Process tasks from queue while the agent is connected.

    Runs until: queue is empty, session stopped, or agent disconnects (CancelledError).
    """
    from .adapters import agent_search_oson, agent_search_arzon, now
    from .geocode import attach_nearest_branches

    log.info('queue worker start agent=%s run=%s', agent_id, run_id)
    while True:
        task = queue.next_task(agent_id)
        if task is None:
            break  # No pending tasks; session done or stopped

        task_id = task['id']
        product_id = task['product_id']
        site = task['site']

        products = store.products([product_id])
        if not products:
            queue.complete_task(task_id, error=f'Product {product_id} not found')
            continue

        product = products[0]
        offers: list = []
        details: dict = {}

        try:
            if site == 'oson' and pinned_bus.connected():
                offers, details = await agent_search_oson(pinned_bus, product)
                if offers:
                    await attach_nearest_branches(offers, store.stores())
            elif site == 'arzon' and pinned_bus.connected():
                region_id = int(os.getenv('ARZON_REGION_ID', '2114'))
                offers, details = await agent_search_arzon(pinned_bus, product, region_id=region_id)
                if offers:
                    await attach_nearest_branches(offers, store.stores())
            else:
                details = {'status': 'skipped', 'error': f'Agent not connected for {site}',
                           'observed_at': now()}

            store.save_result(run_id, product_id, site, details, offers)
            store.update_run(run_id, active_product_id=product_id, active_site=site,
                             phase=f'Queue: {site} done')
            queue.complete_task(task_id)
            log.info('queue task done agent=%s product=%s site=%s status=%s',
                     agent_id, product_id, site, details.get('status'))

        except asyncio.CancelledError:
            log.info('queue worker cancelled agent=%s task=%s will retry', agent_id, task_id)
            raise  # watchdog will reset it to pending after TASK_TIMEOUT

        except Exception as exc:
            log.exception('queue task error agent=%s product=%s site=%s', agent_id, product_id, site)
            err_msg = f'{type(exc).__name__}: {str(exc)[:200]}'
            queue.complete_task(task_id, error=err_msg)
            try:
                store.save_result(run_id, product_id, site,
                                  {'status': 'error', 'error': err_msg, 'observed_at': now()}, [])
            except Exception:
                pass

    log.info('queue worker finished agent=%s', agent_id)
