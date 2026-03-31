"""Market analogue finder — finds similar resolved markets for reference.

Uses TF-IDF cosine similarity to match current market questions against
past resolved markets. Provides Claude with concrete reference points:
"Here's what happened with similar markets and how accurate we were."
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from src.storage.database import Database

logger = logging.getLogger(__name__)

# Refresh corpus every 6 hours (markets resolve infrequently)
CORPUS_REFRESH_INTERVAL = 6 * 3600


@dataclass
class MarketAnalogue:
    """A resolved market similar to the current one."""
    market_id: str
    question: str
    category: str
    similarity_score: float
    predicted_probability: float
    actual_outcome: bool
    brier_score: float
    market_price: float


class AnalogueFinder:
    """Finds resolved markets similar to a given question using TF-IDF."""

    def __init__(self, db: Database):
        self.db = db
        self._corpus: list[dict] = []
        self._corpus_questions: list[str] = []
        self._tfidf_matrix = None
        self._vectorizer = None
        self._last_refresh: float = 0.0

    def _tokenize(self, text: str) -> str:
        """Normalize text for TF-IDF: lowercase, strip punctuation, collapse whitespace."""
        text = text.lower()
        text = re.sub(r'[^\w\s]', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _build_corpus(self) -> None:
        """Load resolved markets and build TF-IDF matrix."""
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
        except ImportError:
            logger.warning("scikit-learn not installed — analogue finder disabled")
            self._corpus = []
            return

        conn = self.db._get_conn()
        rows = conn.execute("""
            SELECT cr.market_id, cr.market_question, cr.predicted_probability,
                   cr.market_price_at_prediction, cr.actual_outcome, cr.brier_score,
                   COALESCE(m.category, 'Other') as category
            FROM calibration_records cr
            LEFT JOIN markets m ON cr.market_id = m.ticker
            WHERE cr.actual_outcome IS NOT NULL
              AND cr.market_question != ''
            ORDER BY cr.resolved_at DESC
        """).fetchall()

        if not rows:
            self._corpus = []
            self._last_refresh = time.monotonic()
            return

        # Deduplicate by market_id (keep first = most recent)
        seen = set()
        unique_rows = []
        for r in rows:
            mid = r["market_id"]
            if mid not in seen:
                seen.add(mid)
                unique_rows.append(dict(r))

        self._corpus = unique_rows
        self._corpus_questions = [self._tokenize(r["market_question"]) for r in unique_rows]

        if len(self._corpus_questions) < 2:
            self._last_refresh = time.monotonic()
            return

        self._vectorizer = TfidfVectorizer(
            max_features=5000,
            stop_words="english",
            ngram_range=(1, 2),
        )
        self._tfidf_matrix = self._vectorizer.fit_transform(self._corpus_questions)
        self._last_refresh = time.monotonic()
        logger.info(f"Analogue finder corpus built: {len(self._corpus)} resolved markets")

    def _ensure_corpus(self) -> None:
        """Rebuild corpus if stale."""
        elapsed = time.monotonic() - self._last_refresh
        if not self._corpus or elapsed > CORPUS_REFRESH_INTERVAL:
            self._build_corpus()

    def find_analogues(
        self,
        question: str,
        category: str = "",
        top_k: int = 5,
        min_similarity: float = 0.15,
    ) -> list[MarketAnalogue]:
        """Find resolved markets most similar to the query question.

        Args:
            question: The market question to match against
            category: Optional category for boosting same-category matches
            top_k: Maximum number of analogues to return
            min_similarity: Minimum cosine similarity threshold

        Returns:
            List of MarketAnalogue sorted by similarity (highest first)
        """
        self._ensure_corpus()

        if not self._corpus or self._vectorizer is None or self._tfidf_matrix is None:
            return []

        try:
            from sklearn.metrics.pairwise import cosine_similarity
        except ImportError:
            return []

        # Transform query
        query_vec = self._vectorizer.transform([self._tokenize(question)])
        similarities = cosine_similarity(query_vec, self._tfidf_matrix).flatten()

        # Boost same-category matches by 20%
        if category:
            for i, record in enumerate(self._corpus):
                if record.get("category", "") == category:
                    similarities[i] *= 1.2

        # Rank and filter
        ranked = sorted(enumerate(similarities), key=lambda x: x[1], reverse=True)

        results = []
        for idx, score in ranked:
            if score < min_similarity:
                break
            if len(results) >= top_k:
                break
            record = self._corpus[idx]
            results.append(MarketAnalogue(
                market_id=record["market_id"],
                question=record["market_question"],
                category=record.get("category", "Other"),
                similarity_score=round(float(score), 3),
                predicted_probability=record["predicted_probability"],
                actual_outcome=bool(record["actual_outcome"]),
                brier_score=record.get("brier_score") or 0.0,
                market_price=record["market_price_at_prediction"],
            ))

        return results

    def format_for_prompt(self, analogues: list[MarketAnalogue]) -> str:
        """Format analogues as a prompt context block.

        Returns empty string if no analogues.
        """
        if not analogues:
            return ""

        lines = ["SIMILAR PAST MARKETS (for reference):"]
        for i, a in enumerate(analogues, 1):
            outcome = "YES" if a.actual_outcome else "NO"
            lines.append(
                f"{i}. \"{a.question[:100]}\" (similarity: {a.similarity_score:.0%})"
            )
            lines.append(
                f"   You predicted: {a.predicted_probability:.0%}, "
                f"Market: {a.market_price:.0%}, "
                f"Actual: {outcome} (Brier: {a.brier_score:.2f})"
            )

        return "\n".join(lines)
