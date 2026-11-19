"""Client for the 03 Dorixona internal service API (staging/production).

Requests must come from the whitelisted server IP (46.101.168.246).
Token is read from DORIXONA_SERVICE_TOKEN env var; URL from DORIXONA_API.
"""
import asyncio
import os

import httpx

API_URL = os.getenv('DORIXONA_API', 'https://api.staging.03dorixona.uz')
TOKEN = os.getenv('DORIXONA_SERVICE_TOKEN', '')

TIMEOUT = 30


def _headers():
    if not TOKEN:
        raise RuntimeError('DORIXONA_SERVICE_TOKEN is not set')
    return {'Authorization': f'Bearer {TOKEN}'}


async def fetch_stores() -> list[dict]:
    """Return list of active stores: [{id, name, address, lat, lng}, ...]."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(f'{API_URL}/api/v1/service/stores', headers=_headers())
        r.raise_for_status()
        return r.json()['items']


async def fetch_stock(store_id: int) -> list[dict]:
    """Return stock for one store: [{product_id, name, qty}, ...]."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(f'{API_URL}/api/v1/service/stores/{store_id}/stock', headers=_headers())
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json()['items']


async def fetch_all_stock(stores: list[dict]) -> dict[int, list[dict]]:
    """Fetch stock for all stores concurrently. Returns {store_id: [items]}."""
    async def one(store):
        items = await fetch_stock(store['id'])
        return store['id'], items

    results = await asyncio.gather(*[one(s) for s in stores], return_exceptions=True)
    out = {}
    for item in results:
        if isinstance(item, Exception):
            continue
        store_id, items = item
        out[store_id] = items
    return out


def build_import(stores: list[dict], stock_by_store: dict[int, list[dict]]) -> tuple[list[dict], list[dict]]:
    """
    Deduplicate products across all pharmacies.

    Returns:
      products  — unique products (one row per product_id), ready for store.import_products()
      pharmacy_stock — flat list of {store_id, store_name, store_address, lat, lng,
                                     dorixona_product_id, name, qty}
                       used to populate the pharmacy_stock table
    """
    seen: dict[int, dict] = {}  # product_id -> product dict
    pharmacy_stock = []

    for store in stores:
        items = stock_by_store.get(store['id'], [])
        for item in items:
            pid = item['product_id']
            if pid not in seen:
                seen[pid] = {
                    'name': item['name'],
                    'manufacturer': '',
                    'invoice_price': None,
                    'barcode': '',
                    'quantity': None,
                    'source_row': None,
                    'sheet': '03Dorixona',
                    'dorixona_product_id': pid,
                }
            pharmacy_stock.append({
                'store_id': store['id'],
                'store_name': store['name'],
                'store_address': store.get('address', ''),
                'lat': store.get('lat'),
                'lng': store.get('lng'),
                'dorixona_product_id': pid,
                'name': item['name'],
                'qty': item['qty'],
            })

    return list(seen.values()), pharmacy_stock
