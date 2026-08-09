import json

import httpx
from pydantic import BaseModel, ValidationError

from app.config import TruthSettings


DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterError(RuntimeError):
    pass


class OpenRouterTimeout(OpenRouterError):
    pass


class OpenRouterMalformedResponse(OpenRouterError):
    pass


class OpenRouterClient:
    """Thin OpenRouter chat-completions client.

    The model id is always supplied by the caller from config
    (TRUTHSEEKER_REASONING_MODEL / TRUTHSEEKER_VERIFIER_MODEL) -- this
    client never hardcodes a specific model, Nemotron or otherwise.
    """

    def __init__(self, settings: TruthSettings, *, api_key: str):
        self.settings = settings
        self.api_key = api_key
        self.base_url = (settings.openrouter_base_url or DEFAULT_BASE_URL).rstrip("/")

    def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        response_schema: type[BaseModel],
        timeout: float,
    ) -> BaseModel:
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise OpenRouterTimeout("OpenRouter request timed out") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise OpenRouterError("OpenRouter request failed") from exc

        try:
            content = payload["choices"][0]["message"]["content"]
            return response_schema.model_validate(json.loads(content))
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            raise OpenRouterMalformedResponse("OpenRouter returned a malformed or unexpected response") from exc
