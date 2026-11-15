import asyncio
from types import SimpleNamespace
import pytest
from dori.browser_config import browser_channel, launch_options
from dori.adapters import BrowserCrawler


def test_default_keeps_bundled_chromium():
    assert browser_channel()=='chromium'
    assert launch_options()=={'headless':True}


def test_interactive_overrides_headless_env(monkeypatch):
    # Even with DORI_HEADLESS=true, interactive (user-controlled viewer) must be non-headless
    # so the site sees a real browser and SafeLine challenges can be solved.
    monkeypatch.setenv('DORI_HEADLESS','true')
    opts = launch_options(interactive=True)
    assert opts['headless'] is False


def test_non_interactive_respects_headless_env(monkeypatch):
    monkeypatch.setenv('DORI_HEADLESS','true')
    assert launch_options()['headless'] is True
    monkeypatch.setenv('DORI_HEADLESS','false')
    assert launch_options()['headless'] is False


def test_branded_chrome_visible_and_proxy(monkeypatch):
    monkeypatch.setenv('DORI_BROWSER_CHANNEL','chrome')
    monkeypatch.setenv('DORI_HEADLESS','false')
    monkeypatch.setenv('DORI_BROWSER_PROXY','http://proxy.example:8080')
    assert launch_options()=={'headless':False,'channel':'chrome','proxy':{'server':'http://proxy.example:8080'}}


def test_invalid_channel_is_not_silently_ignored(monkeypatch):
    monkeypatch.setenv('DORI_BROWSER_CHANNEL','unknown')
    with pytest.raises(ValueError):launch_options()


def test_actual_launch_receives_chrome_channel(monkeypatch,tmp_path):
    monkeypatch.setenv('DORI_BROWSER_CHANNEL','chrome')
    calls={}
    class Context:
        async def close(self):pass
        async def route(self,*args):pass
    class Chromium:
        async def launch_persistent_context(self,profile,**options):
            calls.update(profile=profile,**options)
            return Context()
    class Runtime:
        chromium=Chromium()
        async def stop(self):pass
    class Start:
        async def start(self):return Runtime()
    monkeypatch.setattr('dori.adapters.async_playwright',lambda:Start())
    async def run():
        async with BrowserCrawler(profile=str(tmp_path/'dedicated-profile'),interactive=True):pass
    asyncio.run(run())
    assert calls['channel']=='chrome' and calls['profile'].endswith('dedicated-profile')
