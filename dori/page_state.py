"""Shared page-state categories used by both adapters and the admin panel."""
import re


def has_visible_price(text):
    return bool(re.search(r'\d[\d\s.,]*\s*(?:с[ўу]м|so[ʻ’\x27]?m|UZS)\b', text, re.I))


def classify_text(text):
    """Conservative initial triage; AI assessment refines unfamiliar/ambiguous pages."""
    body=text.casefold()
    # A footer can mention captcha/security on a perfectly valid product page.
    if has_visible_price(text):
        return 'prices_visible'
    if any(x in body for x in ('verify you are human','подтвердите, что вы человек','я не робот',
                              'i am not a robot','проверка браузера','complete the captcha')):
        return 'human_verification'
    if any(x in body for x in ('access forbidden','access denied','security detection','доступ запрещен','доступ запрещён')):
        return 'access_blocked'
    if any(x in body for x in ('too many requests','слишком много запросов','rate limit')):
        return 'rate_limited'
    if any(x in body for x in ('ничего не найдено','ничего не нашли','нет результатов','лекарств: 0','no results')):
        return 'no_results'
    return 'unknown'
