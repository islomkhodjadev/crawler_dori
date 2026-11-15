"""Observe browser-native WAF verification; never fabricate tokens or endpoints."""
import asyncio
import time
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from .page_state import classify_text


@dataclass
class Outcome:
    status: int
    state: str
    automatic: bool = False


def challenge_state(status, text, html):
    state=classify_text(text)
    if status==429:return 'rate_limited'
    if state in ('access_blocked','human_verification'):return state
    if state=='prices_visible' and status not in (401,403):return state
    lower=html.casefold()
    if status in (401,403):return 'access_blocked'
    if status==468 or any(marker in lower for marker in
        ('sl-challenge','leading_zero_bit','/.safeline/','challenge.js','calc.wasm')):
        # A script reference can remain in an otherwise normal page. Only wait
        # on a missing/unknown page, never replace existing prices/no-results.
        if state not in ('prices_visible','no_results'):return 'automatic_verification'
    return state


def retry_after_seconds(value, now=None):
    if not value:return None
    try:
        number=float(value)
        return max(0,number) if math.isfinite(number) else None
    except (TypeError,ValueError):pass
    try:
        target=parsedate_to_datetime(value)
        if target.tzinfo is None:target=target.replace(tzinfo=timezone.utc)
        return max(0,(target-(now or datetime.now(timezone.utc))).total_seconds())
    except (ValueError,TypeError,OverflowError):return None


async def settle(page, response, timeout, progress=None, poll=.4):
    """Let the page's own JS/WASM finish in its current browser session."""
    status=response.status if response else 200
    start_host=urlparse(page.url).hostname
    def observe(r):
        nonlocal status
        if r.request.is_navigation_request() and r.request.frame==page.main_frame:
            status=r.status
    page.on('response',observe)
    deadline=time.monotonic()+timeout
    automatic=False
    try:
        while True:
            if urlparse(page.url).hostname!=start_host:
                return Outcome(status,'unexpected_redirect',automatic)
            try:
                async def snapshot():
                    return await page.locator('body').inner_text(timeout=2000),await page.content()
                text,html=await asyncio.wait_for(snapshot(),timeout=max(.01,min(2,deadline-time.monotonic())))
                state=challenge_state(status,text,html)
            except Exception:
                if time.monotonic()>=deadline:
                    return Outcome(status,'verification_timeout',automatic)
                await asyncio.sleep(poll)
                continue
            if state!='automatic_verification':
                return Outcome(status,state,automatic)
            automatic=True
            if progress:progress('verifying','Waiting for the website’s browser verification (JS/WASM).')
            if time.monotonic()>=deadline:
                return Outcome(status,'verification_timeout',True)
            # No challenge replay, guessed payloads, cookie injection or JS patches.
            await asyncio.sleep(min(poll,max(0,deadline-time.monotonic())))
    finally:
        page.remove_listener('response',observe)
