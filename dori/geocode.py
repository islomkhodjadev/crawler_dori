"""Geocode pharmacy addresses and find nearest 03 Dorixona branch.

Uses Nominatim (OpenStreetMap) with in-process cache.
Only called for per-pharmacy offers (price_kind == 'per_pharmacy').
"""
import asyncio
import math
from typing import Optional
from .logs import get

log = get('geocode')

_cache: dict[str, Optional[tuple[float, float]]] = {}
_lock = asyncio.Lock()


def _haversine(lat1, lon1, lat2, lon2) -> float:
    """Return distance in km between two lat/lon points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def geocode(address: str) -> Optional[tuple[float, float]]:
    """Return (lat, lon) for address string, or None on failure.  Results cached in-process."""
    if not address or not address.strip():
        return None
    key = address.strip().lower()
    async with _lock:
        if key in _cache:
            return _cache[key]
    try:
        import httpx
        params = {
            'q': address,
            'format': 'json',
            'limit': 1,
            'countrycodes': 'uz',
        }
        headers = {'User-Agent': 'DoriPriceComparator/1.0 (pharmacy-price-research)'}
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get('https://nominatim.openstreetmap.org/search', params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
            if data:
                result: Optional[tuple[float, float]] = (float(data[0]['lat']), float(data[0]['lon']))
            else:
                result = None
    except Exception as exc:
        log.debug('geocode failed for %r: %s', address, exc)
        result = None
    async with _lock:
        _cache[key] = result
    return result


def nearest_store(lat: float, lon: float, stores: list[dict]) -> Optional[dict]:
    """Return the store dict with smallest Haversine distance, or None if no stores have lat/lng."""
    best = None
    best_dist = float('inf')
    for s in stores:
        try:
            slat, slon = float(s['lat']), float(s['lng'])
        except (TypeError, KeyError, ValueError):
            continue
        d = _haversine(lat, lon, slat, slon)
        if d < best_dist:
            best_dist = d
            best = s
    return best


async def attach_nearest_branches(offers: list[dict], stores: list[dict]) -> None:
    """Geocode each offer's pharmacy_address and attach nearest_branch in-place.

    Operates only on offers with price_kind == 'per_pharmacy'.
    Skips entries that already have nearest_branch set.
    """
    if not stores:
        return
    tasks = []
    for offer in offers:
        if offer.get('nearest_branch') or offer.get('price_kind') != 'per_pharmacy':
            continue
        addr = offer.get('pharmacy_address', '')
        tasks.append((offer, addr))

    if not tasks:
        return

    # Geocode unique addresses concurrently (Nominatim rate limit: 1 req/s — add delay)
    unique_addrs = list({addr for _, addr in tasks if addr})
    coords: dict[str, Optional[tuple[float, float]]] = {}
    for i, addr in enumerate(unique_addrs):
        if i > 0:
            await asyncio.sleep(1.1)  # Nominatim ToS: max 1 req/s
        coords[addr] = await geocode(addr)

    for offer, addr in tasks:
        pt = coords.get(addr)
        if pt:
            store = nearest_store(pt[0], pt[1], stores)
            if store:
                offer['nearest_branch'] = store['store_name']
                offer['nearest_branch_address'] = store.get('store_address', '')
