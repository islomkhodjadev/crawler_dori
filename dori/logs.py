"""Application logging: one rotating file under data/logs plus the console.

Every AI request and response, page decision, browser action and crawl step is
logged with its context. API keys and bearer tokens are redacted before writing.
Set DORI_LOG_LEVEL=DEBUG to also record page text excerpts sent to the AI.
"""
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER = 'dori'
_SECRET = re.compile(r'(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]+|OPENAI_API_KEY\s*=\s*\S+)')
_configured = None


def redact(value):
    return _SECRET.sub('[redacted]', str(value))


def clip(value, limit=4000):
    text = redact(value)
    return text if len(text) <= limit else text[:limit] + f'… [{len(text) - limit} more chars]'


def get(name):
    return logging.getLogger(f'{LOGGER}.{name}')


def setup(path=None):
    """Configure once per process; repeated calls (tests, reloads) do nothing."""
    global _configured
    if _configured:
        return
    level = getattr(logging, os.getenv('DORI_LOG_LEVEL', 'INFO').upper(), logging.INFO)
    log_path = Path(path or os.getenv('DORI_LOG', 'data/logs/dori.log'))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger(LOGGER)
    root.setLevel(level)
    root.propagate = False
    file_handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s %(name)s: %(message)s'))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s %(message)s', '%H:%M:%S'))
    root.addHandler(file_handler)
    root.addHandler(console)
    _configured = str(log_path)
    root.info('logging started: level=%s file=%s', logging.getLevelName(level), log_path)
