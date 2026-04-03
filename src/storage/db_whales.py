"""Whale tracking and cross-platform pair database operations mixin.

Split from database.py (M-10) to reduce file size while preserving
backward compatibility via the mixin pattern.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class WhalesMixin:
    """Database mixin for whale trade logging and cross-platform pairs."""

    # ──────────────────────────────────────
    # Whale Trades (L-1 audit fix)
    # ──────────────────────────────────────

    def log_whale_trade(
        self,
        wallet_address: str,
        market_id: str,
        direction: str,
        size: float,
        price: float,
        detected_at: str,
    ) -> None:
        """Log a whale trade to the database.

        L-1: Provides a proper abstraction layer so callers don't need
        to access _get_conn() directly.
        """
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO whale_trades (wallet_address, market_id, direction, size, price, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (wallet_address, market_id, direction, size, price, detected_at),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"Failed to log whale trade: {e}")

    def get_whale_activity(self, limit: int = 50) -> list[dict]:
        """Get recent whale trades."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM whale_trades ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ──────────────────────────────────────
    # Cross-Platform Pairs
    # ──────────────────────────────────────

    def upsert_cross_platform_pair(
        self,
        kalshi_ticker: str,
        poly_condition_id: str,
        kalshi_question: str = "",
        poly_question: str = "",
        similarity: float = 0.0,
        validated: bool = False,
    ):
        """Insert or update a cross-platform market pair."""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO cross_platform_pairs
                (kalshi_ticker, poly_condition_id, kalshi_question, poly_question,
                 similarity, validated, validated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kalshi_ticker, poly_condition_id) DO UPDATE SET
                similarity=excluded.similarity,
                validated=excluded.validated,
                validated_at=excluded.validated_at
        """, (
            kalshi_ticker,
            poly_condition_id,
            kalshi_question,
            poly_question,
            similarity,
            int(validated),
            datetime.now(timezone.utc).isoformat() if validated else None,
        ))
        conn.commit()

    def get_cross_platform_pairs(self, validated_only: bool = False) -> list[dict]:
        """Get all cross-platform market pairs."""
        conn = self._get_conn()
        query = "SELECT * FROM cross_platform_pairs"
        if validated_only:
            query += " WHERE validated = 1"
        query += " ORDER BY similarity DESC"
        rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]
