"""Reclassement des passages par un modèle spécialisé (cross-encoder, ex. BAAI/bge-reranker-v2-m3).

La recherche hybride ramène des candidats (mots + sens) ; le reclassement lit chaque couple
(question, passage) en entier et les réordonne : c'est en général le gain de pertinence le plus net.

Service HTTP, deux contrats courants :
  - « tei »  : Text Embeddings Inference de Hugging Face — POST /rerank {query, texts}
               → [{index, score}]
  - « jina » : Jina / Cohere / llama.cpp / Infinity — POST /rerank {model, query, documents, top_n}
               → {results: [{index, relevance_score}]}

Service injoignable : on garde l'ordre de la recherche hybride (journalisé), jamais d'erreur
pour l'utilisateur.
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace

import httpx

from ged.models import Hit

log = logging.getLogger(__name__)

MAX_CHARS = 2000  # passage tronqué pour le modèle (512 jetons environ pour bge-reranker)


class HttpReranker:
    def __init__(self, url: str, *, model: str = "BAAI/bge-reranker-v2-m3", api: str = "tei",
                 candidates: int = 40, timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        if api not in ("tei", "jina"):
            raise ValueError("RERANK_API : « tei » ou « jina »")
        self.url = url.rstrip("/")
        self.model = model
        self.api = api
        self.candidates = max(5, int(candidates))  # passages reclassés par question
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._down_until = 0.0  # après une panne : on n'insiste pas pendant 60 s

    def describe(self) -> str:
        return f"{self.model} ({self.url}, contrat {self.api}, {self.candidates} candidats)"

    def scores(self, query: str, texts: list[str]) -> list[float]:
        if self.api == "tei":
            r = self._http.post(f"{self.url}/rerank", json={"query": query, "texts": texts, "truncate": True})
            r.raise_for_status()
            items = r.json()
            key = "score"
        else:
            r = self._http.post(f"{self.url}/rerank", json={"model": self.model, "query": query, "documents": texts,
                                                           "top_n": len(texts)})
            r.raise_for_status()
            body = r.json()
            items = body.get("results", body) if isinstance(body, dict) else body
            key = "relevance_score"
        out = [0.0] * len(texts)
        for item in items:
            out[int(item["index"])] = float(item.get(key, item.get("score", 0.0)))
        return out

    def rerank(self, query: str, hits: list[Hit]) -> list[Hit]:
        """Réordonne les `candidates` premiers passages ; les suivants restent derrière, dans l'ordre."""
        if not hits or time.monotonic() < self._down_until:
            return hits
        head, tail = hits[: self.candidates], hits[self.candidates:]
        texts = [f"{h.path}\n{h.text}"[:MAX_CHARS] for h in head]
        try:
            scores = self.scores(query, texts)
        except Exception as exc:  # service arrêté, modèle absent : ordre de la recherche hybride
            log.warning("Reclassement indisponible (%s) : ordre de la recherche hybride conservé", exc)
            self._down_until = time.monotonic() + 60
            return hits
        ranked = sorted(zip(head, scores, strict=True), key=lambda hs: hs[1], reverse=True)
        return [replace(h, score=s) for h, s in ranked] + tail

    def check(self) -> tuple[bool, str]:
        try:
            s = self.scores("test", ["passage de test"])
            return True, f"{self.describe()} : opérationnel (score {s[0]:.3f})"
        except Exception as exc:
            return False, f"{self.describe()} : {type(exc).__name__} : {exc}"

    def close(self) -> None:
        self._http.close()


def load_reranker(env=None) -> HttpReranker | None:
    import os

    env = env if env is not None else os.environ
    url = (env.get("RERANK_URL") or "").strip()
    if not url:
        return None
    return HttpReranker(url, model=(env.get("RERANK_MODEL") or "BAAI/bge-reranker-v2-m3").strip(),
                        api=(env.get("RERANK_API") or "tei").strip().lower(),
                        candidates=int(env.get("RERANK_CANDIDATES") or 40))
