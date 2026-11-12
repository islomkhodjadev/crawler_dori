from dori.matching import match

INVOICE = {'name': 'АВАНТОВИР ТАБ 25МГ №30', 'manufacturer': 'AVANTIKA MEDEX PVT. LTD, ИНДИЯ'}


def test_any_manufacturer_listed_by_the_website_can_match():
    offer = {'name': 'Авантовир таб. п/о 25мг №30',
             'manufacturer': 'Avantika Medex (Индия), Avantika Medex Pvt. Ltd. (Индия)'}
    assert match(INVOICE, offer)['status'] == 'accepted'


def test_listed_manufacturers_that_do_not_match_still_conflict():
    offer = {'name': 'Авантовир таб. п/о 25мг №30', 'manufacturer': 'Other Pharma (Индия)'}
    result = match(INVOICE, offer)
    assert result['status'] == 'rejected' and 'manufacturer differs' in result['reasons']


def test_gummies_are_a_different_form_from_capsules():
    product = {'name': 'NOW ВИТАМИН Д3 КАПС 5 000МЕ №120', 'manufacturer': 'NOW FOODS, США'}
    gummies = {'name': 'Витамин Д-3 (NOW Vitamin D-3), 5000 ме, мармелад, №120', 'manufacturer': 'NOW Foods'}
    result = match(product, gummies)
    assert result['status'] == 'rejected' and 'forms differs' in result['reasons']


def test_country_after_manufacturer_is_not_part_of_its_identity():
    product = {'name': 'АКОРА ТАБ 90МГ №10', 'manufacturer': 'ACI LIMITED, БАНГЛАДЕШ'}
    offer = {'name': 'Акора-90, 90 мг, таб. №10', 'manufacturer': 'ACI Limited'}
    assert 'manufacturer differs' not in match(product, offer)['reasons']


def test_comment_explains_differences_in_russian_with_both_values():
    from dori.matching import explain
    product = {'name': 'NOW ВИТАМИН Д3 КАПС 5 000МЕ №120', 'manufacturer': 'NOW FOODS, США'}
    gummies = {'name': 'Витамин Д-3 (NOW Vitamin D-3), 5000 ме, мармелад, №120', 'manufacturer': 'NOW Foods'}
    result = match(product, gummies)
    comment = explain(product, gummies, result['status'], result['reasons'], result['score'])
    assert 'Форма не совпадает: у вас — капсулы, на сайте — жевательная форма (мармелад).' in comment


def test_manufacturer_acronym_matches_its_full_name():
    product = {'name': 'АКОРА ТАБ 90МГ №10', 'manufacturer': 'ACI LIMITED, БАНГЛАДЕШ'}
    offer = {'name': 'Акора таб. п/о 90мг №10',
             'manufacturer': 'Faromed Lifesciences (Индия), Advanced Chemical Industries Ltd (Бангладеш)'}
    assert 'manufacturer differs' not in match(product, offer)['reasons']
    other = dict(offer, manufacturer='Faromed Lifesciences (Индия)')
    assert 'manufacturer differs' in match(product, other)['reasons']
