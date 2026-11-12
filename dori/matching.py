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
         'syrup': r'\b(?:сироп|syrup)\b', 'dragee': r'\b(?:драже|dragee)\b',
         # Gummies and chewables are separate products from ordinary capsules/tablets.
         'chewable': r'\b(?:мармелад\w*|жев(?:ат\w*)?|gumm(?:y|ies)|chewables?)\b'}


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


LEGAL_FORMS = {'ooo','oo','oao','ao','pao','ip','ltd','limited','llc','inc','sa','s','a','pvt','gmbh','ag','jsc','zao'}
# Country names (transliterated Russian and English) are location, not manufacturer identity.
COUNTRIES = {'rossiya','polsha','indiya','uzbekistan','ssha','horvatiya','bangladesh','turtsiya','germaniya',
             'frantsiya','italiya','ispaniya','vengriya','sloveniya','rumyniya','belarus','ukraina','armeniya',
             'kitay','yaponiya','koreya','iran','pakistan','egipet','latviya','litva','estoniya','chehiya',
             'slovakiya','bolgariya','serbiya','avstriya','shveytsariya','niderlandy','belgiya','daniya',
             'shvetsiya','finlyandiya','velikobritaniya','irlandiya','kanada','avstraliya','izrail','iordaniya',
             'kazahstan','gretsiya','portugaliya','india','turkey','germany','usa','uk','china','korea'}


def manufacturer_key(value):
    value = norm(value)
    return ' '.join(w for w in value.split() if w not in LEGAL_FORMS | COUNTRIES)


def same_manufacturer(a, b):
    """Similarity of two manufacturer keys; an acronym equals its spelled-out name (ACI = Advanced Chemical Industries)."""
    def initials(key):
        words = key.split()
        return ''.join(w[0] for w in words) if len(words) >= 2 else ''
    for short, full in ((a, b), (b, a)):
        if len(short) >= 3 and ' ' not in short and short == initials(full):
            return 1.0
    return ratio(a, b) / 100


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
    # Websites list "distributor (country), manufacturer (country)"; any listed party may match.
    ma = manufacturer_key(product.get('manufacturer', ''))
    listed = [k for k in (manufacturer_key(part) for part in re.split(r'[,;]', offer.get('manufacturer', ''))) if k]
    manufacturer_score = max((same_manufacturer(ma, k) for k in listed), default=0) if ma else 0
    if ma and listed and manufacturer_score < .78:
        conflicts.append('manufacturer differs')
    elif not ma or not listed:
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


def summarize_matches(offers, sources):
    from collections import Counter
    per_site={}
    notes=[]
    for site in dict.fromkeys([*sources,*[o['site'] for o in offers]]):
        listings=[o for o in offers if o['site']==site]
        counts={status:sum(o['match_status']==status for o in listings) for status in ('accepted','review','rejected')}
        counts['candidates']=len(listings)
        per_site[site]=counts
        message=f"{site}: {len(listings)} listings, {counts['accepted']} accepted, {counts['review']} review, {counts['rejected']} rejected"
        if counts['accepted']==0:
            reasons=Counter(reason for o in listings for reason in o.get('reasons',[]))
            if reasons:message+=' ('+'; '.join(reason for reason,_ in reasons.most_common(3))+')'
            elif sources.get(site,{}).get('error'):message+=' ('+sources[site]['error']+')'
        notes.append(message)
    accepted=sum(c['accepted'] for c in per_site.values())
    review=sum(c['review'] for c in per_site.values())
    if accepted:status='matched'
    elif review:status='needs_review'
    elif offers:status='rejected'
    elif any(s.get('status')=='error' for s in sources.values()):status='source_error'
    else:status='no_candidates'
    return {'status':status,'per_site':per_site,'notes':' | '.join(notes)}


FORM_NAMES = {'tablet': 'таблетки', 'capsule': 'капсулы', 'drops': 'капли', 'spray': 'спрей', 'cream': 'крем',
              'gel': 'гель', 'solution': 'раствор', 'ointment': 'мазь', 'syrup': 'сироп', 'dragee': 'драже',
              'chewable': 'жевательная форма (мармелад)'}
UNIT_NAMES = {'mg': 'мг', 'iu': 'МЕ', 'ml': 'мл', '%': '%'}


def _forms(values):
    return ', '.join(FORM_NAMES.get(v, v) for v in values) or 'не указана'


def _measures(values):
    def show(unit, value):
        # Grams are stored as milligrams; show them the way packages are labelled.
        if unit == 'mg' and Decimal(value) >= 1000:
            return format((Decimal(value) / 1000).normalize(), 'f') + ' г'
        return f'{value} {UNIT_NAMES.get(unit, unit)}'
    return ', '.join(show(u, v) for u, v in values) or 'не указана'


def explain(product, offer, status, reasons, score, reviewed=False, current_status=None):
    """Plain-Russian comment for the reviewer: what matches, what differs, with both values."""
    a, b = attributes(product['name']), attributes(offer['name'])
    ours = (product.get('manufacturer') or '').strip()
    theirs = (offer.get('manufacturer') or '').strip()
    notes = []
    for reason in reasons:
        if reason == 'forms differs':
            notes.append(f'Форма не совпадает: у вас — {_forms(a["forms"])}, на сайте — {_forms(b["forms"])}.')
        elif reason == 'measures differs':
            notes.append(f'Дозировка не совпадает: у вас — {_measures(a["measures"])}, на сайте — {_measures(b["measures"])}.')
        elif reason == 'pack differs':
            notes.append(f'Количество в упаковке не совпадает: у вас — №{a["pack"]}, на сайте — №{b["pack"]}.')
        elif reason == 'manufacturer differs':
            notes.append(f'Производитель не совпадает: у вас — {ours}, на сайте — {theirs}.')
        elif reason == 'barcode differs':
            notes.append('Штрихкод не совпадает с вашим.')
        elif reason == 'provided site identifier differs':
            notes.append('ID товара на сайте не совпадает с указанным у вас.')
        elif reason == 'forms missing in result':
            notes.append(f'На сайте не указана форма выпуска (у вас — {_forms(a["forms"])}). Проверьте по ссылке.')
        elif reason == 'measures missing in result':
            notes.append(f'На сайте не указана дозировка (у вас — {_measures(a["measures"])}). Проверьте по ссылке.')
        elif reason == 'pack missing in result':
            notes.append(f'На сайте не указано количество в упаковке (у вас — №{a["pack"]}). Проверьте по ссылке.')
        elif reason.endswith(' incomplete') and reason != 'manufacturer incomplete':
            label = {'forms': 'не указана форма выпуска', 'measures': 'не указана дозировка',
                     'pack': 'не указано количество в упаковке'}.get(reason.split()[0], reason)
            notes.append(f'В названии вашего товара {label} — сравнить нельзя, проверьте вручную.')
        elif reason == 'manufacturer incomplete':
            notes.append('Производитель не указан ' + ('на сайте' if ours else 'у вас') + ' — сверьте вручную.')
        elif reason == 'provided identifier matches':
            notes.append('Совпадает указанный вами ID или штрихкод.')
        elif reason.startswith('Page extraction requires review') and not reviewed:
            notes.append('Цена прочитана со страницы автоматически — убедитесь, что цена относится именно к этому товару.')
    labels = {'accepted': 'совпадает', 'review': 'на проверку', 'rejected': 'отклонено'}
    if reviewed:
        notes.insert(0, 'Решение принято вручную: ' + ('подтверждено.' if status == 'accepted' else 'отклонено.'))
    elif current_status and current_status != status:
        notes.insert(0, f'Решение устарело: при сборе — «{labels.get(status, status)}», по текущим правилам — '
                        f'«{labels.get(current_status, current_status)}». Повторите сбор, чтобы обновить.')
    elif status == 'accepted':
        notes.insert(0, 'Совпадает: название, дозировка, форма, упаковка и производитель.')
    elif status == 'rejected' and not any('differs' in r for r in reasons):
        notes.insert(0, f'Название сильно отличается (сходство {round(score * 100)}%) — вероятно, другой товар.')
    elif status == 'review' and not notes:
        notes.append(f'Похоже на ваш товар (сходство названия {round(score * 100)}%), но нужна ручная проверка.')
    price, invoice = offer.get('price'), product.get('invoice_price')
    if price and invoice:
        delta = round((price / invoice - 1) * 100)
        if abs(delta) > 60:
            notes.append(f'Цена отличается от вашей на {delta:+d}% — возможно, цена прочитана неверно; проверьте на сайте.')
    return ' '.join(notes)
