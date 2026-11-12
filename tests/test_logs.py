from dori.logs import clip, redact


def test_api_keys_and_bearer_tokens_are_redacted():
    text = 'Authorization: Bearer abc.def-123 and key sk-proj-ABCDEF123456 and OPENAI_API_KEY=sk-live-9'
    cleaned = redact(text)
    assert 'abc.def' not in cleaned and 'sk-proj' not in cleaned and 'sk-live' not in cleaned
    assert cleaned.count('[redacted]') == 3


def test_long_ai_responses_are_clipped_with_a_count():
    text = clip('x' * 5000, limit=100)
    assert text.startswith('x' * 100) and '4900 more chars' in text
    assert clip('short') == 'short'
