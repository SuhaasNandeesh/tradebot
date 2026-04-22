"""
LM Studio Semantic Embedding Function for ChromaDB.

Uses the locally running LM Studio embedding model (nomic-embed-text-v1.5)
at http://127.0.0.1:1234 to produce 768-dim semantic embeddings.

Why semantic > hash:
  Hash embeddings: "BULLISH market FII buying" ≠ "positive sentiment institutional accumulation"
  Semantic:        Both map to nearby vectors → RAG can recall conceptually similar trades

Falls back to the old hash embedder if LM Studio is not running.
"""
import os
import hashlib
import logging
import requests
from typing import List
from chromadb import EmbeddingFunction, Embeddings

logger = logging.getLogger(__name__)

LMSTUDIO_BASE  = os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234")
EMBED_MODEL    = os.getenv("LMSTUDIO_EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")
EMBED_DIMS     = 768    # nomic-embed-text-v1.5 output dimensions
HASH_DIMS      = 384    # Fallback hash embedding dimensions
TIMEOUT_S      = 10


class LMStudioEmbeddingFunction(EmbeddingFunction):
    """
    ChromaDB-compatible embedding function using LM Studio's local server.
    Primary: nomic-embed-text-v1.5 (768 dims) — semantic, not hash.
    Fallback: deterministic hash (384 dims) if LM Studio is unavailable.
    """

    def __init__(self):
        self._available = None   # Lazily test on first call
        self._session   = requests.Session()

    def _check_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            r = self._session.get(f"{LMSTUDIO_BASE}/v1/models", timeout=3)
            models = [m["id"] for m in r.json().get("data", [])]
            self._available = EMBED_MODEL in models
            if self._available:
                logger.info(f"[LMStudio] Embedding model '{EMBED_MODEL}' available. Using semantic RAG.")
            else:
                logger.warning(f"[LMStudio] Model '{EMBED_MODEL}' not found. Available: {models}")
                logger.warning("[LMStudio] Falling back to hash embeddings.")
        except Exception as e:
            logger.warning(f"[LMStudio] Server not reachable: {e}. Using hash fallback.")
            self._available = False
        return self._available

    def _embed_with_lmstudio(self, texts: List[str]) -> List[List[float]]:
        """Batch embed via LM Studio API."""
        resp = self._session.post(
            f"{LMSTUDIO_BASE}/v1/embeddings",
            json={"model": EMBED_MODEL, "input": texts},
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]

    def _hash_embed(self, text: str) -> List[float]:
        """Deterministic hash-based fallback embedding (not semantic)."""
        h = hashlib.sha256(text.encode()).digest()
        floats = []
        for i in range(0, len(h), 2):
            val = (h[i] * 256 + h[i+1]) / 65535.0
            floats.append(val)
        # Pad or truncate to HASH_DIMS
        floats = (floats * (HASH_DIMS // len(floats) + 1))[:HASH_DIMS]
        return floats

    def __call__(self, input: List[str]) -> Embeddings:
        if self._check_available():
            try:
                # Batch in groups of 32 to avoid API limits
                all_embeddings = []
                for i in range(0, len(input), 32):
                    batch = input[i:i+32]
                    all_embeddings.extend(self._embed_with_lmstudio(batch))
                return all_embeddings
            except Exception as e:
                logger.error(f"[LMStudio] Embedding API error: {e}. Using hash fallback.")
                self._available = False

        # Hash fallback
        return [self._hash_embed(t) for t in input]

    def reset(self):
        """Force re-check of LM Studio availability on next call."""
        self._available = None


# Singleton instance — share across journal calls
_embedder_instance = None

def get_embedder() -> LMStudioEmbeddingFunction:
    global _embedder_instance
    if _embedder_instance is None:
        _embedder_instance = LMStudioEmbeddingFunction()
    return _embedder_instance


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    emb = LMStudioEmbeddingFunction()

    texts = [
        "NIFTY bullish trend with FII buying and strong momentum",
        "Positive market sentiment with institutional accumulation",  # Should be semantically close
        "BEARISH reversal on high VIX and FII selling pressure",      # Should be far
    ]

    vecs = emb(texts)
    print(f"Embedding dims: {len(vecs[0])}")
    print(f"Vectors computed: {len(vecs)}")

    # Cosine similarity to verify semantic closeness
    import math
    def cosine(a, b):
        dot = sum(x*y for x, y in zip(a, b))
        na  = math.sqrt(sum(x**2 for x in a))
        nb  = math.sqrt(sum(x**2 for x in b))
        return dot / (na * nb) if na * nb > 0 else 0

    sim_01 = cosine(vecs[0], vecs[1])
    sim_02 = cosine(vecs[0], vecs[2])
    print(f"\nSimilarity (bull vs bull-paraphrase): {sim_01:.4f}")
    print(f"Similarity (bull vs bear): {sim_02:.4f}")
    assert sim_01 > sim_02, "Semantic: same-direction texts should be more similar"
    print("\nLMStudioEmbeddingFunction PASS ✅")
