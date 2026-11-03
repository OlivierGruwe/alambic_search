"""Calcul des vecteurs. En production : un serveur auto-hébergé compatible OpenAI
(Ollama, vLLM, Text Embeddings Inference). `HashEmbedder` sert uniquement aux tests."""
from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

import httpx


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatEmbedder:
    def __init__(self, base_url: str, model: str, dim: int, batch_size: int = 32, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        self._client = httpx.Client(timeout=timeout)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            resp = self._client.post(f"{self.base_url}/embeddings", json={"model": self.model, "input": batch})
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            for d in data:
                if len(d["embedding"]) != self.dim:
                    raise ValueError(f"Dimension {len(d['embedding'])} reçue, {self.dim} attendue (EMBED_DIM)")
                vectors.append(d["embedding"])
        return vectors


_TOKEN = re.compile(r"\w+", re.UNICODE)


class HashEmbedder:
    """Sac de mots haché, déterministe. Aucune sémantique : pour les tests uniquement."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            v = [0.0] * self.dim
            for tok in _TOKEN.findall(text.lower()):
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
                v[h % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out
