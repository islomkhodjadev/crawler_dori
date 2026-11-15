"""Use an ordinary visible browser and keep user-created session state locally."""
import asyncio
import os
from playwright.async_api import async_playwright
from .browser_config import launch_options


async def open_session(site, profile):
    async with async_playwright() as playwright:
        options = {**launch_options(visible=True), 'locale':'ru-RU'}
        context = await playwright.chromium.launch_persistent_context(profile, **options)
        try:
            page = await context.new_page()
            await page.goto('https://osonapteka.uz/' if site == 'oson' else 'https://arzonapteka.uz/ru',
                            wait_until='domcontentloaded', timeout=60000)
            print('Use the browser to establish a permitted session. No automatic challenge solving is performed.')
            await asyncio.to_thread(input, 'Press Enter here when done to save session and close browser: ')
        finally:
            await context.close()
