"""Pair visible starting prices with their nearest single-product card."""
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup


def parse_oson_cards(html, page_url):
    from .adapters import now, price_value
    soup=BeautifulSoup(html,'html.parser')
    for node in soup.select('script,style,nav,header,footer,[hidden],[aria-hidden="true"]'):
        node.decompose()
    price_nodes=[]
    for node in soup.find_all(['span','p','div','a','strong','b']):
        value=node.get_text(' ',strip=True)
        if re.fullmatch(r'(?:от\s*)?\d[\d\s.,]*\s*(?:с[ўу]м|UZS)',value,re.I) and price_value(value):
            # Retain only the deepest price node, avoiding wrappers counted twice.
            if not any(price_value(c.get_text(' ',strip=True)) for c in node.find_all(['span','p','div','a','strong','b'])):
                price_nodes.append(node)
    result=[]
    for price_node in price_nodes:
        parent=price_node.parent
        for _ in range(7):
            if not parent or parent.name in ('body','html','[document]'):
                break
            if sum(1 for p in price_nodes if parent in p.parents)>1:
                break  # Do not borrow a title from an adjacent medicine.
            candidates=parent.select('[itemprop="name"], h2, h3, h4, h5, h6, strong, b, a[href]')
            candidates += [n for n in parent.find_all(['p','div','span']) if not n.find(['p','div','span'])]
            title=None
            for candidate in candidates:
                name=candidate.get_text(' ',strip=True)
                if (12 <= len(name) <= 600 and price_value(name) is None
                        and re.search(r'(?:№\s*\d|\d\s*(?:мг|мл|ме|mg|ml|iu)|\b(?:таб|капс|крем|спрей))',name,re.I)
                        and not re.match(r'^(подробнее|цены в аптеках|найти|показано)',name,re.I)
                        # Pharmacy headings such as "999 Apteka №1" look like product titles.
                        and not re.search(r'\b(?:apteka|аптека|аптек[ауи]?)\b',name,re.I)):
                    title=candidate
                    break
            if title:
                name=title.get_text(' ',strip=True)
                prefix=name.split(' (',1)[0]
                if re.search(r'№\s*\d|\d\s*(?:мг|мл|mg|ml)',prefix,re.I):
                    name=prefix
                maker=parent.select_one('[itemprop="manufacturer"], [data-manufacturer], .manufacturer')
                manufacturer=(maker.get('data-manufacturer') or maker.get_text(' ',strip=True)) if maker else ''
                # A title with maker appended is retained verbatim; AI can separate it.
                link=title if title.name=='a' else title.find_parent('a',href=True)
                if not link:
                    link=next((a for a in parent.select('a[href]') if a.get_text(' ',strip=True).casefold()=='подробнее'),None)
                url=urljoin(page_url,link.get('href')) if link else page_url
                if urlparse(url).hostname!='osonapteka.uz':
                    break
                result.append(dict(site='oson',site_id='',name=name,manufacturer=manufacturer,
                                   price=price_value(price_node.get_text(' ',strip=True)),currency='UZS',url=url,
                                   observed_at=now(),price_kind='advertised_minimum',
                                   location='Browser session region',extraction='visible_card_requires_review'))
                break
            parent=parent.parent
    return list({(o['name'],o['price'],o['url']):o for o in result}.values())
