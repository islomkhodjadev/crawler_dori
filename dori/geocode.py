"""Find the nearest 03 Dorixona branch for a competitor pharmacy offer.

Two strategies, applied in order:
1. District-name matching — fast, no network call. Extracts district keywords from
   the competitor pharmacy address and matches against store name/address strings.
2. Nominatim geocoding (OpenStreetMap) — async, cached, 1 req/s. Used as fallback
   when district matching finds no store.
"""
import asyncio
import math
import re
from typing import Optional
from .logs import get

log = get('geocode')

_geocode_cache: dict[str, Optional[tuple[float, float]]] = {}
_lock = asyncio.Lock()

# Map of district keyword patterns → canonical district label used in store names.
# Covers Tashkent districts in both Russian and Uzbek spellings.
_DISTRICT_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r'мираб|mirobod', re.I), 'Мирабад'),
    (re.compile(r'юнусаб|yunusob', re.I), 'Юнусабад'),
    (re.compile(r'чилан|chilon', re.I), 'Чиланзар'),
    (re.compile(r'мирзо.улугб|mirzo.ulug', re.I), 'Мирзо-Улугбек'),
    (re.compile(r'яшнаб|yashnob', re.I), 'Яшнабад'),
    (re.compile(r'серг|serg', re.I), 'Сергели'),
    (re.compile(r'беш.?агач|besh.?agach', re.I), 'Бешагач'),
    (re.compile(r'акмал.?абад|akmal', re.I), 'Акмал-Абад'),
    (re.compile(r'шайхант|shayxon', re.I), 'Шайхантахур'),
    (re.compile(r'учтеп|uchtepa', re.I), 'Учтепа'),
    (re.compile(r'алмазар|olmaz', re.I), 'Алмазар'),
    (re.compile(r'ха.?воли|havoli', re.I), 'Хавасти'),
]


def _district_from_address(address: str) -> Optional[str]:
    """Extract a canonical district label from a Russian-language pharmacy address."""
    for pattern, label in _DISTRICT_MAP:
        if pattern.search(address):
            return label
    return None


def _store_matches_district(store: dict, district: str) -> bool:
    """Return True if the store name or address contains the district keyword."""
    haystack = f"{store.get('store_name', '')} {store.get('store_address', '')}".lower()
    return district.lower() in haystack


def _haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nearest_store_by_coords(lat: float, lon: float, stores: list[dict]) -> Optional[dict]:
    best, best_dist = None, float('inf')
    for s in stores:
        try:
            slat, slon = float(s['lat']), float(s['lng'])
        except (TypeError, KeyError, ValueError):
            continue
        d = _haversine(lat, lon, slat, slon)
        if d < best_dist:
            best_dist, best = d, s
    return best


async def _geocode(address: str) -> Optional[tuple[float, float]]:
    """Return (lat, lon) for address string via Nominatim, or None. Results cached."""
    key = address.strip().lower()
    async with _lock:
        if key in _geocode_cache:
            return _geocode_cache[key]
    result: Optional[tuple[float, float]] = None
    try:
        import httpx
        params = {'q': address, 'format': 'json', 'limit': 1, 'countrycodes': 'uz'}
        headers = {'User-Agent': 'DoriPriceComparator/1.0 (pharmacy-price-research)'}
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get('https://nominatim.openstreetmap.org/search', params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
            if data:
                result = (float(data[0]['lat']), float(data[0]['lon']))
    except Exception as exc:
        log.debug('geocode failed for %r: %s', address, exc)
    async with _lock:
        _geocode_cache[key] = result
    return result


async def attach_nearest_branches(offers: list[dict], stores: list[dict]) -> None:
    """Attach nearest_branch + nearest_branch_address to each per-pharmacy offer.

    Uses district-name matching first (instant), falls back to Nominatim geocoding.
    Operates in-place; skips offers that already have nearest_branch set.
    """
    if not stores:
        return

    pending_geocode: list[tuple[dict, str]] = []
    unique_geocode: set[str] = set()

    for offer in offers:
        if offer.get('nearest_branch') or offer.get('price_kind') != 'per_pharmacy':
            continue
        addr = offer.get('pharmacy_address', '')

        # Strategy 1: district keyword match
        district = _district_from_address(addr)
        if district:
            matched = [s for s in stores if _store_matches_district(s, district)]
            if matched:
                # If multiple stores match (same district), pick the one with lat/lng coords (all do);
                # pick the first alphabetically for determinism.
                store = sorted(matched, key=lambda s: s.get('store_name', ''))[0]
                offer['nearest_branch'] = store['store_name']
                offer['nearest_branch_address'] = store.get('store_address', '')
                log.debug('district match %r -> %s', district, store['store_name'])
                continue

        # Strategy 2: Nominatim geocoding
        if addr and addr not in unique_geocode:
            unique_geocode.add(addr)
        pending_geocode.append((offer, addr))

    if not pending_geocode:
        return

    # Geocode unique addresses (rate-limited: 1 req/s per Nominatim ToS)
    coords: dict[str, Optional[tuple[float, float]]] = {}
    for i, addr in enumerate(unique_geocode):
        if i > 0:
            await asyncio.sleep(1.1)
        coords[addr] = await _geocode(addr)

    for offer, addr in pending_geocode:
        if offer.get('nearest_branch'):
            continue
        pt = coords.get(addr)
        if pt:
            store = nearest_store_by_coords(pt[0], pt[1], stores)
            if store:
                offer['nearest_branch'] = store['store_name']
                offer['nearest_branch_address'] = store.get('store_address', '')
