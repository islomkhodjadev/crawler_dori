import json

import pytest

from dori.adapters import agent_search_arzon, agent_search_oson, parse_arzon_pharmacies
from dori.matching import exact_match

PRODUCT = {'name': 'Азитромицин 500 мг таб. №3', 'manufacturer': 'Example Pharma'}
OSON_URL = 'https://osonapteka.uz/product/azithromycin'


@pytest.mark.parametrize('name,maker', [
    ('Азитромицин FFS таб. 500мг №3', 'Example Pharma'),
    ('Азитромицин NP таб. п/о 500мг №3', 'Example Pharma'),
    ('Азитромицин 500 мг таб. №6', 'Example Pharma'),
    ('Азитромицин 500 мг капс. №3', 'Example Pharma'),
    ('Азитромицин 500 мг таб. №3', 'Example Pharma Labs'),
    ('Азитромицин 500 мг таб. №3', ''),
])
def test_exact_match_skips_unverified_or_different_variants(name, maker):
    assert exact_match(PRODUCT, {'name': name, 'manufacturer': maker})['status'] == 'rejected'
    assert exact_match(PRODUCT, {'name': 'Азитромицин, таб. 0,5 г №3', 'manufacturer': 'Example Pharma'})['status'] == 'accepted'


def test_arzon_checkout_never_uses_another_cart_item_or_quantity():
    wrong = '<p>Азитромицин FFS 500 мг таб. №3</p><p>1 шт × 8 600 сўм</p>'
    multi = f'<p>{PRODUCT["name"]}</p><p>2 шт × 48 750 сўм</p>'
    right = f'<p>{PRODUCT["name"]}</p><p>1 шт × 54 000 сўм</p>'
    pharmacy = '<p>Pharmacy</p><p>Адрес: Test Street</p>'
    assert not parse_arzon_pharmacies(pharmacy + wrong + multi, PRODUCT['name'], 'https://arzonapteka.uz')
    assert parse_arzon_pharmacies(pharmacy + wrong + right, PRODUCT['name'], 'https://arzonapteka.uz')[0]['price'] == 54000


@pytest.mark.anyio
async def test_arzon_does_not_open_partial_product():
    class Agent:
        async def fetch(self, url, site):
            return {'html': f'''<article data-product-card="detailed">
            <p>{PRODUCT['name']}</p><p>54 000 сўм</p>
            <a aria-label="{PRODUCT['name']}" href="/ru/products/test-1"></a></article>'''}

        async def fetch_arzon_pharmacies(self, *args):
            pytest.fail('An unverified product page was opened')

    offers, details = await agent_search_arzon(Agent(), {'name': 'Азитромицин 500 мг №3'})
    assert not offers and details['status'] == 'no_results'


@pytest.mark.anyio
@pytest.mark.parametrize('redirected', [False, True])
async def test_oson_opens_exact_listing_and_checks_landed_page(monkeypatch, redirected):
    correct = {**PRODUCT, 'url': OSON_URL, 'site': 'oson'}
    wrong = {**correct, 'name': 'Азитромицин FFS 500 мг таб. №3', 'url': OSON_URL + '-ffs'}

    async def search(*args):
        return [wrong, correct], '<html></html>', 'https://osonapteka.uz/search'

    class Agent:
        async def fetch_oson_pharmacies(self, url, regions):
            assert url == OSON_URL
            return {'url': wrong['url'] if redirected else OSON_URL, 'product_name': PRODUCT['name'],
                    'data': json.dumps({'posList': [{'name': 'Pharmacy', 'minPrice': 54000}]})}

    monkeypatch.setattr('dori.adapters._fetch_oson_page', search)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    offers, details = await agent_search_oson(Agent(), PRODUCT, use_ai=False)
    if redirected:
        assert not offers and details['status'] == 'no_results'
    else:
        assert offers[0]['price'] == 54000 and offers[0]['identity_verified']
