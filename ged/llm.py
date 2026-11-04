"""Appel au LLM qui rédige les réponses : API compatible OpenAI.

- Auto-hébergé : Ollama, vLLM… (LLM_BASE_URL, sans clé).
- Service Mistral AI (France) : LLM_BASE_URL=https://api.mistral.ai/v1, LLM_API_KEY, LLM_MODEL
  (ex. mistral-small-latest). Seuls la question et les extraits que l'utilisateur a le droit de
  lire partent chez le fournisseur ; les vecteurs (EMBED_*) restent calculés en local.
"""
from __future__ import annotations

import logging
import time
from typing import Protocol
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("alambic_search.llm")


class LLM(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class OpenAICompatLLM:
    def __init__(self, base_url: str, model: str, temperature: float = 0.1, timeout: float = 300.0,
                 api_key: str | None = None, retries: int = 2, transport=None, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.retries = retries
        self._sleep = sleep
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(timeout=timeout, headers=headers, transport=transport)

    @property
    def remote(self) -> bool:
        """Service externe (clé d'API) : les extraits quittent l'infrastructure."""
        return "authorization" in self._client.headers

    def describe(self) -> str:
        host = urlsplit(self.base_url).netloc or self.base_url
        return f"{self.model} ({host})"

    def complete(self, system: str, user: str) -> str:
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        for attempt in range(self.retries + 1):
            resp = self._client.post(f"{self.base_url}/chat/completions", json=body)
            # Service en ligne : quota momentanément dépassé (429) ou indisponibilité passagère
            if resp.status_code in (429, 502, 503, 504) and attempt < self.retries:
                wait = min(float(resp.headers.get("retry-after") or 2 ** attempt), 10.0)
                log.warning("LLM %s : HTTP %s, nouvel essai dans %.0f s", self.describe(), resp.status_code, wait)
                self._sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        raise RuntimeError("inaccessible")  # pragma: no cover

    def close(self) -> None:
        self._client.close()


def load_llm(settings) -> OpenAICompatLLM | None:
    """LLM configuré (LLM_MODEL vide : pas de rédaction, extraits seuls)."""
    if not settings.llm_model:
        return None
    return OpenAICompatLLM(settings.llm_base_url, settings.llm_model, api_key=settings.llm_api_key or None)
