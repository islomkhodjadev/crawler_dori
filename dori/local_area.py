"""Distance checks for branch-scoped price comparisons."""
import math


def coordinates(lat, lng):
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if -90 <= lat <= 90 and -180 <= lng <= 180 and (lat or lng):
        return lat, lng
    return None


def pharmacy_coordinates(pharmacy):
    return coordinates(
        next((pharmacy[k] for k in ('latitude', 'lat') if pharmacy.get(k) is not None), None),
        next((pharmacy[k] for k in ('longitude', 'lng', 'lon') if pharmacy.get(k) is not None), None),
    )


def map_link_coordinates(url):
    from urllib.parse import parse_qs, urlsplit

    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    query = parse_qs(parsed.query)
    host = parsed.hostname or ''
    if host in ('yandex.com', 'yandex.ru', 'yandex.uz', 'maps.yandex.ru'):
        pair = query.get('ll', [''])[0].split(',')
        return coordinates(*reversed(pair)) if len(pair) == 2 else None
    if host in ('google.com', 'www.google.com', 'maps.google.com'):
        pair = (query.get('query') or query.get('q') or [''])[0].split(',')
        return coordinates(*pair) if len(pair) == 2 else None
    return None


def address_key(offer):
    address = offer.get('pharmacy_address')
    return (offer['site'], ' '.join(address.casefold().split())) if address and address.strip() else None


def mark_local_offers(offers, center, radius_km, agent_name, known_locations=None):
    from .geocode import _haversine

    for offer in offers:
        point = None
        if offer.get('price_kind') == 'per_pharmacy':
            point = (known_locations or {}).get(address_key(offer)) or pharmacy_coordinates(offer)
            if point:
                offer['latitude'], offer['longitude'] = point
        distance = _haversine(*center, *point) if point else None
        offer.update(
            distance_km=round(distance, 3) if distance is not None else None,
            location_status=('unknown' if distance is None else 'nearby' if distance <= radius_km else 'outside'),
            search_lat=center[0], search_lng=center[1],
            radius_km=radius_km, search_agent=agent_name,
        )


def eligible_for_comparison(offer):
    return offer.get('location_status') in (None, 'nearby')
