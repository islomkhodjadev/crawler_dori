from dori.oson_cards import parse_oson_cards

PRODUCT_PAGE = '''<html><body>
<div class="pharmacy-card">
  <div class="head"><h3>999 Apteka №1</h3><span class="price">7 000 сум</span></div>
  <p>Наманган, город Наманган</p>
  <div class="offer"><span>Антисептик Sofderm 3в1 спрей 100мл №1 (Uniderm ООО (Узбекистан))</span><span>7 000 сум</span></div>
</div></body></html>'''


def test_pharmacy_heading_is_not_taken_as_product_name():
    offers = parse_oson_cards(PRODUCT_PAGE, 'https://osonapteka.uz/search?q=sofderm')
    names = {o['name'] for o in offers}
    assert not any('Apteka' in name for name in names)
    assert 'Антисептик Sofderm 3в1 спрей 100мл №1' in names
    assert {o['price'] for o in offers} == {7000.0}
