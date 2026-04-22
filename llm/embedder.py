"""文本嵌入服务 — text-embedding-v4 via DashScope。"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


class Embedder:
    """Batch text embedding using DashScope text-embedding-v4.

    Reads configuration from environment variables:
        EMBEDDING_API_KEY: DashScope API key.
        EMBEDDING_BASE_URL: API base URL (default: DashScope compatible endpoint).
        EMBEDDING_MODEL: Model name (default: text-embedding-v4).
    """

    def __init__(self) -> None:
        self._api_key = os.environ.get("EMBEDDING_API_KEY", "")
        self._base_url = os.environ.get(
            "EMBEDDING_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        self._model = os.environ.get("EMBEDDING_MODEL", "text-embedding-v4")
        self._batch_size = 20  # API limit per request

    def is_configured(self) -> bool:
        """Check if API key is set.

        Returns:
            True if EMBEDDING_API_KEY is non-empty.
        """
        return bool(self._api_key)

    async def embed(self, text: str) -> list[float] | None:
        """Embed a single text.

        Args:
            text: Input text to embed.

        Returns:
            1024-dimensional float vector, or None on failure.
        """
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        """Embed multiple texts in batches of 20.

        Splits the input into batches of at most ``self._batch_size``, calls the
        API for each batch, and reassembles results in the original order.

        Args:
            texts: List of input texts to embed.

        Returns:
            List of 1024-dimensional vectors with the same length as *texts*.
            Individual items are None when their embedding failed.
        """
        if not texts:
            return []

        results: list[list[float] | None] = [None] * len(texts)

        for batch_start in range(0, len(texts), self._batch_size):
            batch = texts[batch_start : batch_start + self._batch_size]
            try:
                vectors = await self._call_api(batch)
                for i, vec in enumerate(vectors):
                    results[batch_start + i] = vec
            except Exception as exc:
                logger.warning(
                    "embed_batch_failed",
                    batch_start=batch_start,
                    batch_size=len(batch),
                    error=str(exc),
                )
                # Leave those slots as None

        return results

    async def _call_api(self, texts: list[str]) -> list[list[float]]:
        """Call DashScope embedding API for a single batch.

        Sends a POST request to ``{base_url}/embeddings`` with the given texts
        and parses the response, sorting embeddings by their ``index`` field
        before returning.

        Args:
            texts: List of texts (≤ 20 items).

        Returns:
            List of 1024-dimensional float vectors in the same order as *texts*.

        Raises:
            RuntimeError: If the API returns a non-200 status or an unexpected
                response body.
            httpx.RequestError: On network-level failures.
        """
        url = f"{self._base_url.rstrip('/')}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._model,
            "input": texts,
            "dimensions": 1024,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Embedding API returned {resp.status_code}: {resp.text[:200]}"
                )
            data: dict[str, Any] = resp.json()

        items: list[dict[str, Any]] = data.get("data", [])
        if not items:
            raise RuntimeError(
                f"Embedding API returned empty data field. Response: {data}"
            )

        # Sort by index to guarantee order matches the input
        items_sorted = sorted(items, key=lambda x: x["index"])
        return [item["embedding"] for item in items_sorted]
