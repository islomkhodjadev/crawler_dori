"""Operator tools: environment health checks and live collection progress."""
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

STALL_AFTER = timedelta(minutes=10)
FINISHED = ('completed', 'failed', 'interrupted')


def _age(stamp):
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


def doctor(db_path=None):
    """Return (name, level, detail) rows; level is OK, WARN or FAIL. Never prints secrets."""
    rows = []
    def add(name, level, detail):
        rows.append((name, level, detail))

    add('python', 'OK' if sys.version_info >= (3, 11) else 'FAIL', sys.version.split()[0] + ' (needs 3.11+)')

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
        if os.path.exists(executable):
            add('chromium', 'OK', 'installed')
        else:
            add('chromium', 'FAIL', 'missing; run: python -m playwright install chromium')
    except Exception as exc:
        add('chromium', 'FAIL', f'Playwright unavailable: {type(exc).__name__}')

    add('admin password', 'OK' if os.getenv('DORI_ADMIN_PASSWORD') else 'WARN',
        'set' if os.getenv('DORI_ADMIN_PASSWORD') else 'not set; panel is only safe on this computer')
    add('OpenAI key', 'OK' if os.getenv('OPENAI_API_KEY') else 'WARN',
        'configured' if os.getenv('OPENAI_API_KEY') else 'not set; AI parsing and assessment are disabled')

    path = Path(db_path or os.getenv('DORI_DB', 'data/dori.sqlite3'))
    if not path.exists():
        add('database', 'WARN', f'{path} does not exist yet; it is created on first start')
        return rows
    try:
        # Read-only: a diagnostic must not run migrations or change the database.
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5) as db:
            db.row_factory = sqlite3.Row
            integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = {'products', 'runs', 'results', 'offers'} - tables
            if missing:
                add('database', 'FAIL', f'integrity={integrity}; missing tables={sorted(missing)}')
                return rows
            products = db.execute('SELECT count(*) FROM products').fetchone()[0]
            stale = db.execute("SELECT id,status,last_activity FROM runs WHERE status IN ('queued','running')").fetchall()
            waiting = db.execute("SELECT id FROM runs WHERE status='waiting_for_human'").fetchall()
            latest = db.execute('SELECT id,status FROM runs ORDER BY id DESC LIMIT 1').fetchone()
    except sqlite3.Error as exc:
        add('database', 'FAIL', f'cannot read {path}: {exc}')
        return rows
    if integrity != 'ok':
        add('database', 'FAIL', f'integrity check: {integrity}')
    else:
        add('database', 'OK', f'{products} imported product(s)')

    for run in stale:
        age = _age(run['last_activity'])
        if age is not None and age > STALL_AFTER:
            add(f'run {run["id"]}', 'FAIL', f'{run["status"]} but silent for {int(age.total_seconds() // 60)} min; stop it or restart the app')
    if waiting:
        add('verification', 'WARN', 'run(s) ' + ', '.join(str(r['id']) for r in waiting) + ' waiting for a human to complete verification')
    if latest:
        add('latest run', 'OK', f'run {latest["id"]}: {latest["status"]}')
    else:
        add('latest run', 'OK', 'no crawl has been started yet')
    return rows


def print_doctor(rows):
    for name, level, detail in rows:
        print(f'{level:<4}  {name:<18} {detail}')
    failed = sum(level == 'FAIL' for _, level, _ in rows)
    print(f'\n{failed} failing check(s).' if failed else '\nNo failing checks.')
    return 1 if failed else 0


def snapshot(store, run_id=None):
    """Human-readable progress for one run, built from the same statistics the admin panel shows."""
    stats = store.statistics(run_id)
    run = stats['run']
    if not run:
        return 'No crawl runs yet. Start one from the admin panel or with: dori crawl'
    products = stats['products']
    listings = stats['listings']
    lines = [
        f"Run {run['id']}: {run['status']} — {run.get('phase') or ''}",
        f"  products  {products['completed']}/{products['selected']} ({stats['progress_percent']}%), "
        f"matched {products['matched']}, completed without match {products['completed_without_match']}",
        f"  listings  collected {listings['total']}, accepted {listings['accepted']}, "
        f"review {listings['review']}, rejected {listings['rejected']}",
    ]
    if stats['active']:
        lines.append(f"  active    {stats['active']['name']} @ {stats['active']['site']}")
    for source in stats['sources']:
        lines.append(f"  {source['site']:<8}  checks {source['checks_completed']}, ok {source['successful']}, "
                     f"no results {source['no_results']}, errors {source['errors']}")
    errors = [e for e in stats['events'] if e['status'] not in ('ok', 'no_results')][:5]
    for event in errors:
        lines.append(f"  ! {event['site']} {event['name'][:50]}: {event['message'][:120]}")
    if run.get('error'):
        lines.append(f"  error     {run['error'][:200]}")
    return '\n'.join(lines)


def watch(store, run_id=None, interval=5, once=False):
    """Print progress until the run finishes (or once). Returns 0 when it finished cleanly."""
    while True:
        text = snapshot(store, run_id)
        print(text + '\n', flush=True)
        run = store.statistics(run_id)['run']
        if once or not run or run['status'] in FINISHED:
            return 0 if not run or run['status'] == 'completed' else 1
        time.sleep(interval)
