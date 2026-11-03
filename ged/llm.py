"""Appel au LLM auto-hébergé (API compatible OpenAI : Ollama, vLLM...)."""
from __future__ import annotations

from typing import Protocol

import httpx


class LLM(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class OpenAICompatLLM:
    def __init__(self, base_url: str, model: str, temperature: float = 0.1, timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self._client = httpx.Client(timeout=timeout)

    def complete(self, system: str, user: str) -> str:
        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "temperature": self.temperature,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
