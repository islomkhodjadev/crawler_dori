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
         'chewable': r'\b(?:мармелад\w*|жев(?:ат\w*)?|gumm(?:y|ies)|chewables?)\b',
         # Cosmetic/hygiene delivery forms — distinct from pharma sprays, creams, etc.
         'shower': r'\b(?:душ|dusch|shower)\b',
         'lotion': r'\b(?:лосьон|lotion)\b',
         'shampoo': r'\b(?:шампунь|shampoo)\b',
         'foam': r'\b(?:пена|foam)\b',
         'patch': r'\b(?:пласт(?:ырь)?|patch)\b',
         'suppository': r'\b(?:свеч(?:и|а)|supp(?:ositories?)?)\b',
         'powder': r'\b(?:порош(?:ок)?|powder)\b',
         'suspension': r'\b(?:сусп(?:ензия)?|suspension)\b',
         'injection': r'\b(?:ампул\w*|инъекц\w*|injection|ampoule?s?)\b'}


def norm(s):
    s = unicodedata.normalize('NFKC', s).casefold().replace('ё', 'е')
    return re.sub(r'[^a-z0-9]+', ' ', s.translate(TRANSLIT)).strip()


def attributes(name, *, strict=False):
    s = unicodedata.normalize('NFKC', name).casefold().replace(',', '.')
    # Strip parenthetical manufacturer/brand annotations like "(NOW Vitamin D-3)" or "(Now Foods (США))"
    # that sites append to the drug name — they pollute the brand score.
    # Two passes handles one level of nesting: inner first, then outer.
    if not strict:
        for _ in range(2):
            s = re.sub(r'\([^()]{2,60}\)', ' ', s)
    # Normalize reversed percent notation: "%5" → "5%" so the unit extractor finds it.
    s = re.sub(r'%\s*(\d+(?:\.\d+)?)\b', r'\1%', s)
    # Collapse grouped numeric thousands such as "5 000МЕ" or "10 000мг" before unit extraction.
    # Only collapse when the trailing group is exactly 3 digits followed by a non-digit (a unit or space).
    s = re.sub(r'(\d)\s+(?=\d{3}(?:[^0-9]|$))', r'\1', s)
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
    if strict:
        clean = clean.replace('/', ' per ')
    else:
        clean = re.sub(r'\b(?:п/о|флак|назал|д/лица|д/тела)\b', ' ', clean)
    parsed = dict(brand=norm(clean), pack=pack, measures=sorted(measures), forms=forms)
    if strict:
        parsed['pack_layout'] = tuple(int(part) for part in count.groups() if part) if count else ()
        parsed['measure_order'] = measures
    return parsed


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


def _measures_compatible(a_measures, b_measures) -> bool:
    """Return True if measures are compatible despite being unequal.

    Competitor listings for liquids (syrups, solutions, injections) often include
    both concentration (mg/ml) AND total volume (ml), while our DB entry has only
    volume. Example: our "100 мл" vs competitor "15мг/5мл 100мл №1" → both have
    ('ml', '100') so they describe the same product. We treat this as compatible
    when every measure in the smaller set is present in the larger set.
    """
    if not a_measures or not b_measures:
        return False
    a_set, b_set = set(map(tuple, a_measures)), set(map(tuple, b_measures))
    # Both must share at least one measure, and the smaller must be a subset
    return bool(a_set & b_set) and (a_set <= b_set or b_set <= a_set)


_VARIANT_RE = re.compile(
    r'\b(?:плюс|plus|макс|max|extra|ультра|ultra|нео|neo|актив|activ(?:e)?|про|pro)\b',
    re.I | re.U,
)


def _variant_modifiers(name: str) -> frozenset:
    """Extract brand-variant words from raw name (before transliteration) that distinguish product lines."""
    s = unicodedata.normalize('NFKC', name).casefold().replace('ё', 'е')
    return frozenset(m for m in _VARIANT_RE.findall(s))


def match(product, offer):
    a, b = attributes(product['name']), attributes(offer['name'])
    reasons, conflicts, missing = [], [], []
    score = round(ratio(a['brand'], b['brand']) / 100, 3)
    # Variant modifiers like "плюс/plus" mark distinct product lines — if one side has
    # them and the other doesn't, it's a different product regardless of name similarity.
    va, vb = _variant_modifiers(product['name']), _variant_modifiers(offer['name'])
    if va != vb:
        conflicts.append('variant modifier differs')
    for key in ('pack', 'measures', 'forms'):
        if key == 'measures' and a[key] and b[key] and a[key] != b[key]:
            if _measures_compatible(a[key], b[key]):
                # Competitor has more detail (e.g. concentration+volume vs volume only)
                missing.append('measures incomplete')
            else:
                conflicts.append('measures differs')
        elif a[key] and b[key] and a[key] != b[key]:
            conflicts.append(f'{key} differs')
        elif a[key] and not b[key]:
            missing.append(f'{key} missing in result')
        elif not a[key] or not b[key]:
            missing.append(f'{key} incomplete')
    # Websites list "distributor (country), manufacturer (country)"; any listed party may match.
    ma = manufacturer_key(product.get('manufacturer') or '')
    listed = [k for k in (manufacturer_key(part) for part in re.split(r'[,;]', offer.get('manufacturer') or '')) if k]
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


def identity_label(name, manufacturer):
    annotation = re.search(r'\s+\(([^()]+(?:\s*\([^()]+\))?)\)$', name)
    maker = manufacturer_key(manufacturer or '')
    if annotation and maker and manufacturer_key(annotation[1]) == maker:
        return name[:annotation.start()]
    return name


def same_product_label(a, b):
    """Compare full product labels with normalized units and formatting."""
    left, right = attributes(a, strict=True), attributes(b, strict=True)
    return bool(left['brand']) and left == right


def exact_match(product, offer):
    """Require positive evidence of the same brand, manufacturer and package."""
    decision = match(product, offer)
    if decision['status'] == 'rejected':
        return decision
    reasons = []
    if not same_product_label(identity_label(product['name'], product.get('manufacturer')),
                              identity_label(offer['name'], offer.get('manufacturer'))):
        reasons.append('Exact name or package differs')
    maker = manufacturer_key(product.get('manufacturer') or '')
    if not maker or maker != manufacturer_key(offer.get('manufacturer') or ''):
        reasons.append('Exact manufacturer is unverified')
    if offer.get('price_kind') == 'per_pharmacy' and not offer.get('identity_verified'):
        reasons.append('Product page or pharmacy price association is unverified')
    if decision['status'] != 'accepted':
        reasons.extend(decision['reasons'])
    if reasons:
        return {**decision, 'status': 'rejected', 'reasons': reasons}
    return decision


def search_term(product, site=None):
    if product.get('barcode'):
        return product['barcode']
    # Include brand name + dosage form word so search engines return the right form.
    name = product['name']
    # Strip leading rank-prefix digits like "1 ", "2 ", "7 " from internal product names.
    name = re.sub(r'^\d+\s+', '', name).strip()
    # Fix common internal spelling errors before searching.
    name = re.sub(r'АСКВРБИНКА', 'АСКОРБИНКА', name, flags=re.I)
    FORM_WORDS = r'таб|капс|спрей|крем|гель|капли|сироп|мазь|р-р|раствор|суспензия|суспен|свечи|свеч|порошок|пор|ампул|амп|флак'
    # Find form word position, include it in term
    m_form = re.search(r'\b(' + FORM_WORDS + r')\b', name, re.I)
    if m_form:
        # Include everything up to and including the form word
        term = name[:m_form.end()].strip(' ,.-')
    else:
        # No form word — split at first standalone digit (not part of a letter+digit token like D3 or Д3)
        parts = re.split(r'(?<![а-яёА-ЯЁa-zA-Z])\d', name, maxsplit=1)
        term = parts[0].strip(' ,.-% ') or name.split()[0]
    if site == 'oson':
        # Oson's search engine maps short Latin-only tokens (e.g. "NOW") to Cyrillic near-matches.
        # Drop leading Latin-only words so the Cyrillic drug name drives the search.
        # Then re-attach any letter+digit suffix (e.g. "Д3") that the split cut off.
        words = term.split()
        cyrillic_start = next((i for i, w in enumerate(words) if re.search(r'[а-яёА-ЯЁ]', w)), None)
        if cyrillic_start and cyrillic_start > 0:
            term = ' '.join(words[cyrillic_start:])
        # If the original name has a letter+digit token right after the term, re-attach it
        # (e.g. "ВИТАМИН Д" → "ВИТАМИН Д3" when the name contains "Д3").
        m = re.search(re.escape(term.rstrip()) + r'(\d+)', name, re.I)
        if m:
            term = term.rstrip() + m.group(1)
    return term


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
              'chewable': 'жевательная форма (мармелад)', 'shower': 'средство для душа',
              'lotion': 'лосьон', 'shampoo': 'шампунь', 'foam': 'пена', 'patch': 'пластырь',
              'suppository': 'свечи', 'powder': 'порошок', 'suspension': 'суспензия',
              'injection': 'инъекция/ампулы'}
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
        if reason == 'variant modifier differs':
            va2, vb2 = _variant_modifiers(product['name']), _variant_modifiers(offer['name'])
            only_ours = ', '.join(sorted(va2 - vb2))
            only_theirs = ', '.join(sorted(vb2 - va2))
            if only_ours and only_theirs:
                notes.append(f'Вариант препарата не совпадает: у вас — «{only_ours}», на сайте — «{only_theirs}».')
            elif only_ours:
                notes.append(f'У вас указан вариант «{only_ours}», на сайте такого обозначения нет — возможно, другой препарат.')
            else:
                notes.append(f'На сайте указан вариант «{only_theirs}», у вас такого обозначения нет — возможно, другой препарат.')
        elif reason == 'forms differs':
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
