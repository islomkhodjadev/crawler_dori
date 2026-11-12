import os
import tempfile

import pytest

# Keep test runs out of the operator's real crawl log.
os.environ.setdefault('DORI_LOG', os.path.join(tempfile.mkdtemp(), 'test.log'))

# app.py loads the developer's .env at import time, so a local admin password or
# API key would leak into tests and change their behaviour. Clear every setting
# the application reads; each test sets only what it needs.
CONFIG_VARS = (
    'DORI_ADMIN_PASSWORD', 'DORI_ADMIN_PROFILES', 'DORI_DB', 'DORI_BROWSER_PROFILE',
    'DORI_BROWSER_PROXY', 'DORI_HEADLESS', 'OPENAI_API_KEY', 'OPENAI_MODEL',
    'OPENAI_PARSE_HTML', 'CRAWL_DELAY_SECONDS', 'CRAWL_MAX_PAGES',
)


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    for name in CONFIG_VARS:
        monkeypatch.delenv(name, raising=False)
