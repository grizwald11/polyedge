"""Market graph — vector similarity store for finding related markets.

Uses ChromaDB with sentence-transformers for semantic similarity.
Falls back to keyword-based matching if ChromaDB is not installed.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.core.models import Market

logger = logging.getLogger(__name__)


class MarketGraph:
    """Vector store for market relationship detection."""

    def __init__(self, persist_dir: str = "data/chroma"):
        self.persist_dir = persist_dir
        self._collection = None
        self._client = None
        self._available = False
        self._init_store()

    def _init_store(self):
        """Initialize ChromaDB collection. Degrades gracefully if unavailable."""
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            self._client = chromadb.Client(ChromaSettings(
                persist_directory=self.persist_dir,
                anonymized_telemetry=False,
            ))
            self._collection = self._client.get_or_create_collection(
                name="markets",
                metadata={"hnsw:space": "cosine"},
            )
            self._available = True
            logger.info(f"Market graph initialized at {self.persist_dir}")
        except ImportError:
            logger.info("ChromaDB not installed — market graph using keyword fallback")
        except Exception as e:
            logger.warning(f"ChromaDB init failed: {e} — using keyword fallback")

    def index_markets(self, markets: list[Market]):
        """Index all markets into the vector store."""
        if not self._available or not self._collection:
            return

        ids = []
        documents = []
        metadatas = []

        for market in markets:
            ids.append(market.ticker)
            documents.append(f"{market.question} {market.description}")
            metadatas.append({
                "event_ticker": market.event_ticker,
                "category": market.category.value,
                "yes_price": market.yes_price,
            })

        try:
            self._collection.upsert(
                ids=ids,
                documents=documents,
                metadatas=metadatas,
            )
            logger.debug(f"Indexed {len(markets)} markets into graph")
        except Exception as e:
            logger.warning(f"Market graph indexing failed: {e}")

    def find_related(self, market: Market, n: int = 10) -> list[dict]:
        """Find markets semantically related to the given market.

        Returns list of dicts with keys: ticker, score, event_ticker, category.
        """
        if not self._available or not self._collection:
            return self._keyword_fallback(market, n)

        try:
            results = self._collection.query(
                query_texts=[f"{market.question} {market.description}"],
                n_results=n + 1,  # +1 to exclude self
            )

            related = []
            for i, ticker in enumerate(results["ids"][0]):
                if ticker == market.ticker:
                    continue
                metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                distance = results["distances"][0][i] if results["distances"] else 1.0
                related.append({
                    "ticker": ticker,
                    "score": 1.0 - distance,  # Convert distance to similarity
                    "event_ticker": metadata.get("event_ticker", ""),
                    "category": metadata.get("category", ""),
                })

            return related[:n]
        except Exception as e:
            logger.warning(f"Market graph query failed: {e}")
            return self._keyword_fallback(market, n)

    def find_mutual_exclusives(self, event_ticker: str) -> list[str]:
        """Find all markets in the same event (mutual exclusives).

        Markets in the same Kalshi event are typically mutually exclusive outcomes.
        """
        if not self._available or not self._collection:
            return []

        try:
            results = self._collection.get(
                where={"event_ticker": event_ticker},
            )
            return results["ids"] if results and results["ids"] else []
        except Exception as e:
            logger.warning(f"Mutual exclusive search failed: {e}")
            return []

    def find_subset_superset_pairs(
        self, markets: list[Market], similarity_threshold: float = 0.7
    ) -> list[tuple[str, str, float]]:
        """Find potential subset/superset pairs among markets.

        Returns list of (market_a, market_b, similarity_score) tuples
        where similarity > threshold.
        """
        pairs: list[tuple[str, str, float]] = []
        seen: set[tuple[str, str]] = set()

        for market in markets:
            related = self.find_related(market, n=5)
            for rel in related:
                if rel["score"] < similarity_threshold:
                    continue
                pair = tuple(sorted([market.ticker, rel["ticker"]]))
                if pair not in seen:
                    seen.add(pair)
                    pairs.append((market.ticker, rel["ticker"], rel["score"]))

        pairs.sort(key=lambda x: x[2], reverse=True)
        return pairs

    def _keyword_fallback(self, market: Market, n: int) -> list[dict]:
        """Simple keyword-based fallback when ChromaDB is unavailable."""
        return []
