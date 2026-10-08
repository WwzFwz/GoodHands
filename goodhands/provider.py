"""Non-streaming Chat Completions adapter; credentials never enter run artifacts."""

import os
from typing import Protocol
from urllib.parse import urlparse

import httpx

from .models import Profile, ProviderSettings


class ProviderError(RuntimeError):
    pass


class Provider(Protocol):
    simulated: bool

    def complete(
        self, *, profile: Profile, messages: list[dict], tools: list[dict], stage: str
    ) -> dict: ...


class ChatProvider:
    simulated = False

    def __init__(self, settings: ProviderSettings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client

    def preflight(self):
        url = urlparse(self.settings.base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query:
            raise ValueError(
                "Live provider base_url must be HTTPS without embedded credentials or query"
            )
        if not os.environ.get(self.settings.api_key_env):
            raise ValueError(
                f"Set {self.settings.api_key_env} in your local environment; never put the key in task files."
            )

    def complete(
        self, *, profile: Profile, messages: list[dict], tools: list[dict], stage: str
    ) -> dict:
        self.preflight()
        if profile.model == "SET_MODEL_ID" or not profile.model.strip():
            raise ValueError("Configure a real model ID in profiles before making a live request")
        body = {
            "model": profile.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": profile.max_output_tokens,
            "stream": False,
        }
        if self.settings.kind == "openrouter":
            body["provider"] = {"require_parameters": True}
        headers = {
            "Authorization": "Bearer " + os.environ[self.settings.api_key_env],
            "X-OpenRouter-Title": "GoodHands",
        }
        try:
            if self.client:
                response = self.client.post(
                    self.settings.base_url.rstrip("/") + "/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=self.settings.timeout_seconds,
                )
            else:
                with httpx.Client(follow_redirects=False, trust_env=False) as client:
                    response = client.post(
                        self.settings.base_url.rstrip("/") + "/chat/completions",
                        json=body,
                        headers=headers,
                        timeout=self.settings.timeout_seconds,
                    )
        except httpx.HTTPError:
            raise ProviderError(
                "Provider connection failed or timed out. Charge is unknown; reservation retained. Resume deliberately to retry."
            ) from None
        if response.status_code >= 300:
            explanation = {
                401: "check API credentials",
                402: "API credit is required",
                403: "provider access denied",
                429: "rate limit reached",
            }.get(response.status_code, "provider request failed")
            raise ProviderError(
                f"HTTP {response.status_code}: {explanation}. Reservation retained; no automatic replay."
            )
        try:
            data = response.json()
        except ValueError:
            raise ProviderError("Provider returned invalid JSON; reservation retained") from None
        if not isinstance(data, dict) or "error" in data:
            raise ProviderError("Provider returned an error payload; reservation retained")
        return data
