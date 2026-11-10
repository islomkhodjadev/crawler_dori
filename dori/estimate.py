from collections import defaultdict
from statistics import median


def estimate(offers, invoice_price=None):
    """Equal source weight prevents one aggregator from dominating another."""
    sites = defaultdict(dict)
    for offer in offers:
        if offer.get('match_status') == 'accepted' and offer.get('currency') == 'UZS' and offer['price'] > 0:
            # Deduplicate identical listings; never combine different products implicitly.
            sites[offer['site']][offer['url']] = offer['price']
    per_site = {site: round(median(values.values()), 2) for site, values in sites.items()}
    value = round(median(per_site.values()), 2) if per_site else None
    prices = [price for listings in sites.values() for price in listings.values()]
    both = 'oson' in per_site and 'arzon' in per_site
    return dict(value=value, currency='UZS', per_site=per_site,
                minimum=min(prices) if prices else None, maximum=max(prices) if prices else None,
                site_count=len(per_site), accepted_listings=sum(len(v) for v in sites.values()),
                comparison_available=both,
                status='two_sources' if both else 'single_source' if value is not None else 'unavailable',
                spread_percent=round((max(per_site.values()) / min(per_site.values()) - 1) * 100, 2) if both else None,
                invoice_delta_percent=round((value / invoice_price - 1) * 100, 2) if value is not None and invoice_price and invoice_price > 0 else None,
                method='Median of per-site median accepted listing prices. Search cards are advertised starting prices, not the full retail distribution.')
