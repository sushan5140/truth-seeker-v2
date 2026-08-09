import pytest
import time
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import SETTINGS
from app.main import create_app
from app.models import GeminiToneResult
from app.services import tone_checker
from app.services.truth.pipeline import ClaimVerificationService


@pytest.fixture
def api_truth_service(tmp_path, monkeypatch):
    settings = replace(SETTINGS, cache_path=tmp_path / "api-claim-cache.sqlite3")
    service = ClaimVerificationService(settings)
    monkeypatch.setattr(service.verifier, "api_key", "")
    return service


@pytest.fixture
def client(api_truth_service, monkeypatch):
    """Keep every ordinary test local and quota-free."""
    def unavailable(_text, _api_key, _sensitivity):
        raise RuntimeError("mock Gemini unavailable")

    monkeypatch.setattr(tone_checker, "_generate_with_gemini", unavailable)
    with TestClient(create_app(api_truth_service)) as test_client:
        yield test_client


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["backend_running"] is True
    assert isinstance(response.json()["gemini_configured"], bool)


def test_empty_claim_input(client):
    response = client.post("/api/check-claim", json={"text": ""})
    assert response.status_code == 422
    assert response.json()["status"] == "invalid"


def test_claim_longer_than_5000_characters(client):
    response = client.post("/api/check-claim", json={"text": "a" * 5001})
    assert response.status_code == 422
    assert "5,000" in response.json()["message"]


def test_opinion_detection(client):
    response = client.post("/api/check-claim", json={"text": "I think summer is the best season."})
    assert response.status_code == 200
    assert response.json()["verdict"] == "opinion"
    assert response.json()["evidence"] == []


def test_default_insufficient_evidence_result_without_gemini(client):
    response = client.post("/api/check-claim", json={"text": "The city has twelve public parks."})
    data = response.json()
    assert response.status_code == 200
    assert data["verdict"] == "insufficient_evidence"
    assert data["provider"]["verifier"] in {"not_used", "local_fallback"}
    assert any("not prove" in limitation.lower() or "not inferred" in limitation.lower() for limitation in data["limitations"])


def test_api_tests_use_temporary_cache_without_touching_configured_cache(client, api_truth_service):
    configured_path = SETTINGS.cache_path
    existed_before = configured_path.exists()
    contents_before = configured_path.read_bytes() if existed_before else None

    assert api_truth_service.cache.path != configured_path
    assert api_truth_service.cache.path.parent != configured_path.parent
    response = client.post("/api/check-claim", json={"text": "The city has twelve public parks."})

    assert response.status_code == 200
    assert api_truth_service.cache.path.exists()
    assert configured_path.exists() is existed_before
    if existed_before:
        assert configured_path.read_bytes() == contents_before


def test_hostile_phrase_detection(client):
    response = client.post("/api/check-tone", json={"text": "That is a stupid idea."})
    assert response.status_code == 200
    assert response.json()["intervention_needed"] is True
    assert response.json()["confidence"] == "high"
    assert response.json()["problematic_phrase"] == "stupid"


def test_neutral_tone(client):
    message = "I disagree because the evidence is incomplete."
    response = client.post("/api/check-tone", json={"text": message})
    assert response.status_code == 200
    assert response.json()["intervention_needed"] is False
    assert response.json()["confidence"] == "low"
    assert response.json()["suggested_rewrite"] == message


def test_rewrite_preserves_disagreement(client):
    response = client.post("/api/check-tone", json={"text": "This idea is nuts and will never work."})
    suggestion = response.json()["suggested_rewrite"].lower()
    assert response.status_code == 200
    assert "not convinced" in suggestion
    assert "work" in suggestion


@pytest.mark.parametrize(
    "message, phrase",
    [
        ("You are an idiot.", "idiot"),
        ("This idea is stupid.", "stupid"),
        ("Shut up, you know nothing.", "shut up"),
        ("This is completely useless.", "useless"),
        ("Your suggestion is ridiculous.", "ridiculous"),
        ("This idea is nuts and will never work.", "nuts"),
    ],
)
def test_high_confidence_hostile_examples(client, message, phrase):
    response = client.post("/api/check-tone", json={"text": message})
    data = response.json()
    assert response.status_code == 200
    assert data["intervention_needed"] is True
    assert data["confidence"] == "high"
    assert data["problematic_phrase"] == phrase


@pytest.mark.parametrize(
    "message",
    [
        "I disagree with this idea.",
        "I don’t think this will work.",
        "This result looks incorrect.",
        "Can you explain this again?",
        "That was a difficult test.",
        "The nuts are on the table.",
        "The system is not useful for my case.",
        "I am writing about the word stupid.",
        "He said, ‘this idea is ridiculous.’",
    ],
)
def test_harmless_examples_remain_silent(client, message):
    response = client.post("/api/check-tone", json={"text": message})
    data = response.json()
    assert response.status_code == 200
    assert data["intervention_needed"] is False
    assert data["confidence"] == "low"
    assert data["suggested_rewrite"] == message


@pytest.mark.parametrize("message", ["stupid", "This idea is stupid", "You are an idiot"])
def test_keyword_only_or_incomplete_text_remains_silent(client, message):
    response = client.post("/api/check-tone", json={"text": message})
    assert response.status_code == 200
    assert response.json()["intervention_needed"] is False


def test_valid_gemini_structured_response(client, monkeypatch):
    result = GeminiToneResult(
        intervention_needed=True,
        category="dismissive_language",
        confidence="high",
        problematic_phrase="This idea is stupid",
        suggestion="I don't think this idea will work. Could we examine its limitations?",
        explanation="The wording may sound dismissive.",
    )
    monkeypatch.setattr(tone_checker, "_generate_with_gemini", lambda _text, _key, _sensitivity: result)
    response = client.post("/api/check-tone", json={"text": "This idea is stupid."})
    data = response.json()
    assert response.status_code == 200
    assert data["provider"] == "gemini"
    assert data["intervention_needed"] is True
    assert data["confidence"] == "high"


def test_gemini_normal_disagreement_stays_silent(client, monkeypatch):
    message = "I don’t think this approach will work because it is too expensive."
    result = GeminiToneResult(
        intervention_needed=False,
        category="neutral_criticism",
        confidence="high",
        problematic_phrase=None,
        suggestion=message,
        explanation="This is a normal disagreement.",
    )
    monkeypatch.setattr(tone_checker, "_generate_with_gemini", lambda _text, _key, _sensitivity: result)
    data = client.post("/api/check-tone", json={"text": message}).json()
    assert data["provider"] == "gemini"
    assert data["intervention_needed"] is False
    assert data["suggested_rewrite"] == message


def test_malformed_gemini_response_uses_fallback(client, monkeypatch):
    monkeypatch.setattr(tone_checker, "_generate_with_gemini", lambda _text, _key, _sensitivity: (_ for _ in ()).throw(ValueError("invalid JSON")))
    data = client.post("/api/check-tone", json={"text": "You are an idiot."}).json()
    assert data["provider"] == "local_fallback"
    assert data["intervention_needed"] is True


def test_missing_key_uses_fallback(client, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    data = client.post("/api/check-tone", json={"text": "You are an idiot."}).json()
    assert data["provider"] == "local_fallback"


def test_timeout_uses_fallback_quickly(client, monkeypatch):
    def timeout(_text, _api_key, _sensitivity):
        raise TimeoutError("mock timeout")

    monkeypatch.setattr(tone_checker, "_generate_with_gemini", timeout)
    started = time.perf_counter()
    data = client.post("/api/check-tone", json={"text": "This idea is stupid."}).json()
    elapsed = time.perf_counter() - started
    assert data["provider"] == "local_fallback"
    assert elapsed < 0.5


def test_rewrite_preserves_the_disagreement_with_gemini(client, monkeypatch):
    result = GeminiToneResult(
        intervention_needed=True,
        category="dismissive_language",
        confidence="high",
        problematic_phrase="nuts",
        suggestion="I'm not convinced this idea would work in practice. Could we examine its limitations?",
        explanation="The wording may sound dismissive.",
    )
    monkeypatch.setattr(tone_checker, "_generate_with_gemini", lambda _text, _key, _sensitivity: result)
    data = client.post("/api/check-tone", json={"text": "This idea is nuts and will never work."}).json()
    assert "not convinced" in data["suggested_rewrite"].lower()
    assert "work" in data["suggested_rewrite"].lower()


def test_stale_and_duplicate_request_guards_exist_without_worker_cancellation():
    project = Path(__file__).resolve().parents[2]
    content_script = (project / "extension" / "content.js").read_text(encoding="utf-8")
    background = (project / "extension" / "background.js").read_text(encoding="utf-8")
    assert "currentRequest !== requestNumber" in content_script
    assert "lastRequestedText.get(editor) === text" in content_script
    assert 'type: "CANCEL_TONE"' not in content_script
    assert "activeToneRequests" not in background
    assert 'message?.type === "CHECK_TONE"' in background
    assert "return { ok: true, data };" in background


def test_sensitivity_levels_have_distinct_fallback_behavior(client, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    clear_insult = "You are an idiot."
    strong_dismissal = "This idea is stupid."
    mild_dismissal = "This is a waste of time."

    for sensitivity in ("low", "balanced", "high"):
        data = client.post("/api/check-tone", json={"text": clear_insult, "sensitivity": sensitivity}).json()
        assert data["intervention_needed"] is True

    assert client.post("/api/check-tone", json={"text": strong_dismissal, "sensitivity": "low"}).json()["intervention_needed"] is False
    assert client.post("/api/check-tone", json={"text": strong_dismissal, "sensitivity": "balanced"}).json()["intervention_needed"] is True
    assert client.post("/api/check-tone", json={"text": strong_dismissal, "sensitivity": "high"}).json()["intervention_needed"] is True

    assert client.post("/api/check-tone", json={"text": mild_dismissal, "sensitivity": "low"}).json()["intervention_needed"] is False
    assert client.post("/api/check-tone", json={"text": mild_dismissal, "sensitivity": "balanced"}).json()["intervention_needed"] is False
    assert client.post("/api/check-tone", json={"text": mild_dismissal, "sensitivity": "high"}).json()["intervention_needed"] is True


def test_invalid_sensitivity_is_rejected(client):
    response = client.post("/api/check-tone", json={"text": "You are an idiot.", "sensitivity": "maximum"})
    assert response.status_code == 422


def test_selected_sensitivity_is_sent_to_gemini(client, monkeypatch):
    observed = {}
    result = GeminiToneResult(
        intervention_needed=False,
        category="neutral",
        confidence="high",
        problematic_phrase=None,
        suggestion="This is a normal disagreement.",
        explanation="No intervention is needed.",
    )

    def capture(_text, _key, sensitivity):
        observed["sensitivity"] = sensitivity
        return result

    monkeypatch.setattr(tone_checker, "_generate_with_gemini", capture)
    response = client.post("/api/check-tone", json={"text": "This is a normal disagreement.", "sensitivity": "high"})
    assert response.status_code == 200
    assert response.json()["provider"] == "gemini"
    assert observed["sensitivity"] == "high"


def test_editor_support_and_floating_card_controls_are_preserved():
    project = Path(__file__).resolve().parents[2]
    content_script = (project / "extension" / "content.js").read_text(encoding="utf-8")
    for selector in ('textarea', 'input[type="text"]', '[contenteditable="true"]', '[role="textbox"]', '.ProseMirror'):
        assert selector in content_script
    assert "new MutationObserver" in content_script
    for button in ('makeButton("Replace"', 'makeButton("Ignore"', 'makeButton("Dismiss"'):
        assert button in content_script
    assert "TruthToneSettings.shouldAnalyze" in content_script


def test_popup_settings_and_diagnostic_controls_are_present():
    project = Path(__file__).resolve().parents[2]
    popup_html = (project / "extension" / "popup" / "popup.html").read_text(encoding="utf-8")
    popup_js = (project / "extension" / "popup" / "popup.js").read_text(encoding="utf-8")
    assert 'id="automatic-tone-guard"' in popup_html
    assert 'id="tone-sensitivity"' in popup_html
    assert 'id="diagnostic-button"' in popup_html
    assert "TruthToneSettings.save" in popup_js
    assert 'type: "RUN_BACKEND_SELF_TEST"' in popup_js


def test_api_key_is_absent_from_frontend_files():
    project = Path(__file__).resolve().parents[2]
    env_text = (project / "backend" / ".env").read_text(encoding="utf-8").strip()
    secret = env_text.split("=", 1)[1]
    assert secret
    frontend_text = "\n".join(path.read_text(encoding="utf-8") for path in (project / "extension").rglob("*") if path.is_file())
    assert secret not in frontend_text
    assert "GEMINI_API_KEY=" not in frontend_text
