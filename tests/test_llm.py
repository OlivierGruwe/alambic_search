"""LLM : auto-hébergé (Ollama) ou en ligne (Mistral AI, clé d'API)."""
from __future__ import annotations

import json

import httpx
import pytest

from ged.config import Settings
from ged.llm import OpenAICompatLLM, load_llm


def test_mistral_key_and_retry_on_quota():
    seen, statuses = [], iter([429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        code = next(statuses)
        if code == 429:
            return httpx.Response(429, headers={"retry-after": "1"}, json={"message": "Requests rate limit exceeded"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Réponse [1]."}}]})

    waits = []
    llm = OpenAICompatLLM("https://api.mistral.ai/v1/", "mistral-small-latest", api_key="cle-secrete",
                          transport=httpx.MockTransport(handler), sleep=waits.append)
    assert llm.complete("système", "question") == "Réponse [1]."
    assert waits == [1.0] and len(seen) == 2
    assert seen[0].url == "https://api.mistral.ai/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer cle-secrete"
    assert json.loads(seen[0].content)["model"] == "mistral-small-latest"
    assert llm.remote and llm.describe() == "mistral-small-latest (api.mistral.ai)"


def test_persistent_error_is_raised():
    llm = OpenAICompatLLM("https://api.mistral.ai/v1", "m", api_key="k", sleep=lambda s: None,
                          transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "Unauthorized"})))
    with pytest.raises(httpx.HTTPStatusError):
        llm.complete("s", "u")


def test_local_llm_has_no_key(monkeypatch):
    for k in ("LLM_BASE_URL", "LLM_API_KEY", "EMBED_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMBED_BASE_URL", "http://ollama:11434/v1")
    s = Settings.from_env()
    assert s.llm_base_url == "http://ollama:11434/v1" and s.llm_api_key == ""  # même serveur que les vecteurs
    assert not load_llm(s).remote
    monkeypatch.setenv("LLM_API_KEY", "aucune")  # valeur du secret Docker sans LLM en ligne
    assert Settings.from_env().llm_api_key == ""
    monkeypatch.setenv("LLM_BASE_URL", "https://api.mistral.ai/v1")
    monkeypatch.setenv("LLM_API_KEY", "cle")
    monkeypatch.setenv("LLM_MODEL", "mistral-small-latest")
    llm = load_llm(Settings.from_env())
    assert llm.remote and llm.base_url == "https://api.mistral.ai/v1"
    monkeypatch.setenv("LLM_MODEL", "")
    assert load_llm(Settings.from_env()) is None
