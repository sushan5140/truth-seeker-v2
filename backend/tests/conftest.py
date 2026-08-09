import pytest


@pytest.fixture(autouse=True)
def _no_real_provider_credentials(request, monkeypatch):
    """backend/.env has a real GEMINI_API_KEY (and possibly a real
    OPENROUTER_API_KEY) for local dev use. Strip both from the environment
    before every test by default, so a test that forgets to stub its
    verifier can never make a live model API call.

    A handful of Tone Guard tests (test_api.py) intentionally need the real
    GEMINI_API_KEY present -- they pass tone_checker's own
    is_gemini_configured() check on purpose, while mocking the actual
    generation call (_generate_with_gemini) separately, so no live call
    happens even though the key is present. Those tests opt out explicitly
    with @pytest.mark.needs_real_gemini_key rather than being silently
    exempted here.
    """
    if request.node.get_closest_marker("needs_real_gemini_key"):
        return
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
