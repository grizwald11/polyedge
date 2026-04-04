"""Market graph — vector similarity store for finding related markets.

Uses ChromaDB with sentence-transformers for semantic similarity.
Falls back to keyword-based matching if ChromaDB is not installed.
"""

from __future__ import annotations

import logging

from src.core.models import Market

logger = logging.getLogger(__name__)


class MarketGraph:
    """Vector store for market relationship detection."""

    def __init__(self, persist_dir: str = "data/chroma"):
        self.persist_dir = persist_dir
        self._collection = None
        self._client = None
        self._available = False
        self._indexed_markets: list[Market] = []  # Fallback store for keyword matching
        self._init_store()

    def _init_store(self):
        """Initialize ChromaDB collection. Degrades gracefully if unavailable."""
        try:
            import chromadb

            # Use PersistentClient for disk-backed storage (avoids singleton
            # conflict that chromadb.Client() has when called multiple times
            # with different persist_directory settings).
            self._client = chromadb.PersistentClient(
                path=self.persist_dir,
            )
            self._collection = self._client.get_or_create_collection(
                name="markets",
                metadata={"hnsw:space": "cosine"},
            )
            self._available = True
            logger.info(f"Market graph initialized at {self.persist_dir}")
        except ImportError:
            logger.warning("ChromaDB not installed — market graph using keyword fallback (reduced accuracy for arb detection)")
        except Exception as e:
            logger.warning(f"ChromaDB init failed: {e} — using keyword fallback (reduced accuracy for arb detection)")

    def index_markets(self, markets: list[Market]):
        """Index all markets into the vector store."""
        # Always store for keyword fallback
        self._indexed_markets = markets

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

            ids = results["ids"][0] if results["ids"] else []
            metadatas = results["metadatas"][0] if results.get("metadatas") else []
            distances = results["distances"][0] if results.get("distances") else []

            related = []
            for i, ticker in enumerate(ids):
                if ticker == market.ticker:
                    continue
                metadata = metadatas[i] if i < len(metadatas) else {}
                distance = distances[i] if i < len(distances) else 1.0
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
        """Keyword-based fallback when ChromaDB is unavailable.

        Uses Jaccard similarity on word sets from market questions.
        """
        if not self._indexed_markets:
            return []

        query_words = self._tokenize(f"{market.question} {market.description}")
        if not query_words:
            return []

        scored: list[tuple[str, float, Market]] = []
        for other in self._indexed_markets:
            if other.ticker == market.ticker:
                continue
            other_words = self._tokenize(f"{other.question} {other.description}")
            if not other_words:
                continue
            # Jaccard similarity
            intersection = len(query_words & other_words)
            union = len(query_words | other_words)
            score = intersection / union if union > 0 else 0.0
            if score > 0.1:  # Minimum 10% overlap
                scored.append((other.ticker, score, other))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [
            {
                "ticker": ticker,
                "score": score,
                "event_ticker": m.event_ticker,
                "category": m.category.value,
            }
            for ticker, score, m in scored[:n]
        ]

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Tokenize text into a set of lowercase words (3+ chars), including hyphenated terms."""
        import re

        # Match words and hyphenated compounds (e.g., "anti-trust", "re-election")
        words = set(re.findall(r'\b[a-z]{3,}(?:-[a-z]{3,})*\b', text.lower()))
        # Remove common stop words
        stop_words = {
            "the", "and", "for", "that", "this", "will", "with", "from",
            "not", "are", "was", "has", "have", "had", "but", "can",
            "its", "all", "any", "each", "than", "what", "when", "how",
        }
        return words - stop_words
