"""Conservative package identity matching, not therapeutic substitution."""
import re
import unicodedata
from decimal import Decimal
from rapidfuzz.fuzz import ratio

TRANSLIT = str.maketrans(dict(zip(
    'абвгдеёзийклмнопрстуфхыэ',
    ['a','b','v','g','d','e','e','z','i','y','k','l','m','n','o','p','r','s','t','u','f','h','y','e'])))
TRANSLIT.update(str.maketrans({'ж':'zh','ц':'ts','ч':'ch','ш':'sh','щ':'shch','ю':'yu','я':'ya','ь':'','ъ':''}))
FORMS = {'tablet': r'\b(?:таб(?:летки|летка)?|tab(?:lets?)?)\b',
         'capsule': r'\b(?:капс(?:улы|ула)?|caps?(?:ules?)?)\b',
         'drops': r'\b(?:капли|капель|drops?)\b', 'spray': r'\b(?:спрей|spray)\b',
         'cream': r'\b(?:крем|cream)\b', 'gel': r'\b(?:гель|gel)\b',
         'solution': r'\b(?:раствор|р-р|solution)\b', 'ointment': r'\b(?:мазь|ointment)\b',
         'syrup': r'\b(?:сироп|syrup)\b', 'dragee': r'\b(?:драже|dragee)\b'}


def norm(s):
    s = unicodedata.normalize('NFKC', s).casefold().replace('ё', 'е')
    return re.sub(r'[^a-z0-9]+', ' ', s.translate(TRANSLIT)).strip()


def attributes(name):
    s = unicodedata.normalize('NFKC', name).casefold().replace(',', '.')
    # Collapse grouped numeric thousands such as "5 000МЕ" before unit extraction.
    s = re.sub(r'(\d)\s+(?=\d{3}(?:\D|$))', r'\1', s)
    count = re.search(r'(?:№|\b[nn]o?\.?\s*|#)\s*(\d+)(?:\s*[xх*]\s*(\d+))?', s)
    pack = int(count[1]) * int(count[2] or 1) if count else None
    if count:
        s = s[:count.start()] + ' ' + s[count.end():]
    measures = []
    unitmap = {'мг': ('mg', 1), 'mg': ('mg', 1), 'г': ('mg', 1000), 'g': ('mg',1000),
               'мкг': ('mg', .001), 'mcg': ('mg', .001), 'µg': ('mg', .001),
               'ме': ('iu', 1), 'iu': ('iu', 1), 'мл': ('ml', 1), 'ml': ('ml',1), '%': ('%',1)}
    pattern = r'(\d+(?:\.\d+)?)\s*(мкг|mcg|µg|мг|mg|мл|ml|ме|iu|г|g|%)(?![а-яa-z])'
    def extract(m):
        unit, factor = unitmap[m[2]]
        measures.append((unit, format((Decimal(m[1]) * Decimal(str(factor))).normalize(), 'f')))
        return ' '
    clean = re.sub(pattern, extract, s)
    forms = sorted(key for key, pattern in FORMS.items() if re.search(pattern, s))
    for pattern in FORMS.values():
        clean = re.sub(pattern, ' ', clean)
    clean = re.sub(r'\b(?:п/о|флак|назал|д/лица|д/тела)\b', ' ', clean)
    return dict(brand=norm(clean), pack=pack, measures=sorted(measures), forms=forms)


def manufacturer_key(value):
    value = norm(value)
    return ' '.join(w for w in value.split() if w not in {
        'ooo','oo','oao','ao','pao','ip','ltd','limited','llc','inc','sa','s','a','pvt',
        'rossiya','polsha','indiya','uzbekistan','ssha','horvatiya'})


def match(product, offer):
    a, b = attributes(product['name']), attributes(offer['name'])
    reasons, conflicts, missing = [], [], []
    score = round(ratio(a['brand'], b['brand']) / 100, 3)
    for key in ('pack', 'measures', 'forms'):
        if a[key] and b[key] and a[key] != b[key]:
            conflicts.append(f'{key} differs')
        elif a[key] and not b[key]:
            missing.append(f'{key} missing in result')
        elif not a[key] or not b[key]:
            missing.append(f'{key} incomplete')
    ma, mb = manufacturer_key(product.get('manufacturer', '')), manufacturer_key(offer.get('manufacturer', ''))
    manufacturer_score = ratio(ma, mb) / 100 if ma and mb else 0
    if ma and mb and manufacturer_score < .78:
        conflicts.append('manufacturer differs')
    elif not ma or not mb:
        missing.append('manufacturer incomplete')
    # Exact external identity is supporting evidence, never a reason to ignore conflicts.
    id_key = {'arzon': 'arzon_id', 'oson': 'oson_id'}.get(offer.get('site'))
    identity = bool(id_key and product.get(id_key) and product[id_key] == offer.get('site_id'))
    barcode = bool(product.get('barcode') and product['barcode'] == offer.get('barcode'))
    if identity or barcode:
        reasons.append('provided identifier matches')
    if product.get('barcode') and offer.get('barcode') and product['barcode'] != offer['barcode']:
        conflicts.append('barcode differs')
    if id_key and product.get(id_key) and offer.get('site_id') and product[id_key] != offer['site_id']:
        conflicts.append('provided site identifier differs')
    if conflicts:
        status = 'rejected'
    elif a['brand'] == b['brand'] and not missing:
        status = 'accepted'
    elif (identity or barcode) and not missing:
        status = 'accepted'
    elif score >= .65 or identity or barcode:
        status = 'review'
    else:
        status = 'rejected'
    if status == 'accepted' and offer.get('extraction') in ('ai_requires_review','visible_card_requires_review'):
        status = 'review'
        reasons.append('Page extraction requires review of product/price association')
    return dict(status=status, score=score, reasons=conflicts + missing + reasons or ['name and package attributes agree'])


def search_term(product):
    if product.get('barcode'):
        return product['barcode']
    # Include brand words until dosage/form so that variants can be compared locally.
    name = product['name']
    parts = re.split(r'\b(?:таб|капс|спрей|крем|гель|капли|сироп|мазь|р-р|раствор)\b|\d', name, maxsplit=1, flags=re.I)
    return parts[0].strip(' ,.-') or name.split()[0]
