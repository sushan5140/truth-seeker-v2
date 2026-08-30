import json
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import TruthSettings


DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class ToolCallResult(BaseModel):
    """Result of a tools-enabled chat completion. content is the model's
    final free-text answer (tool calls are resolved server-side by
    OpenRouter before this returns -- there's no separate tool_calls
    round-trip for the caller to drive). citations is best-effort: OpenRouter
    surfaces fetched/searched URLs via url_citation annotations on the
    message, but the exact annotation shape isn't pinned down in public docs,
    so extraction is defensive and simply omits anything it doesn't
    recognize rather than raising."""

    content: str
    citations: list[str] = Field(default_factory=list)


def _extract_citations(message: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for annotation in message.get("annotations") or []:
        if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
            continue
        nested = annotation.get("url_citation")
        url = nested.get("url") if isinstance(nested, dict) else annotation.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")) and url not in seen:
            seen.append(url)
    return seen


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

    def complete_with_tools(
        self,
        *,
        model: str,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        timeout: float,
    ) -> ToolCallResult:
        """Free-text (non-JSON-schema) completion with OpenRouter server
        tools attached via the `tools` array (`openrouter:web_search`,
        `openrouter:web_fetch`, etc.) -- OpenRouter executes any tool calls
        server-side and returns only the final assistant message, so this
        is a single request/response, same shape as complete_json's call,
        just without response_format/schema validation."""
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
                    "tools": tools,
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
            message = payload["choices"][0]["message"]
            content = message["content"]
            if not isinstance(content, str):
                raise TypeError("message content is not a string")
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenRouterMalformedResponse("OpenRouter returned a malformed or unexpected response") from exc

        return ToolCallResult(content=content, citations=_extract_citations(message))
