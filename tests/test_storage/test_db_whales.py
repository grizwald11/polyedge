"""Tests for WhalesMixin (db_whales.py): whale trades, cross-platform pairs."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------------
# Whale Trades
# ---------------------------------------------------------------------------


class TestLogWhaleTradeAndGetActivity:
    def test_log_and_retrieve(self, tmp_db):
        tmp_db.log_whale_trade(
            wallet_address="0xABCD",
            market_id="MKT-1",
            direction="BUY_YES",
            size=500.0,
            price=0.45,
            detected_at="2026-04-03T10:00:00+00:00",
        )
        activity = tmp_db.get_whale_activity(limit=10)
        assert len(activity) == 1
        row = activity[0]
        assert row["wallet_address"] == "0xABCD"
        assert row["market_id"] == "MKT-1"
        assert row["direction"] == "BUY_YES"
        assert row["size"] == 500.0
        assert row["price"] == 0.45

    def test_ordering_descending_by_detected_at(self, tmp_db):
        tmp_db.log_whale_trade("0xA", "MKT-1", "BUY_YES", 100, 0.50, "2026-04-01T00:00:00+00:00")
        tmp_db.log_whale_trade("0xB", "MKT-2", "BUY_NO", 200, 0.60, "2026-04-03T00:00:00+00:00")
        tmp_db.log_whale_trade("0xC", "MKT-3", "BUY_YES", 300, 0.70, "2026-04-02T00:00:00+00:00")
        activity = tmp_db.get_whale_activity(limit=10)
        assert len(activity) == 3
        # Most recent first
        assert activity[0]["wallet_address"] == "0xB"
        assert activity[1]["wallet_address"] == "0xC"
        assert activity[2]["wallet_address"] == "0xA"

    def test_limit_parameter(self, tmp_db):
        for i in range(5):
            tmp_db.log_whale_trade(
                f"0x{i}", "MKT-1", "BUY_YES", 100, 0.50,
                f"2026-04-0{i+1}T00:00:00+00:00",
            )
        activity = tmp_db.get_whale_activity(limit=3)
        assert len(activity) == 3

    def test_empty_activity(self, tmp_db):
        activity = tmp_db.get_whale_activity()
        assert activity == []


# ---------------------------------------------------------------------------
# Cross-Platform Pairs
# ---------------------------------------------------------------------------


class TestCrossPlatformPairs:
    def test_upsert_and_retrieve(self, tmp_db):
        tmp_db.upsert_cross_platform_pair(
            kalshi_ticker="FED-RATE-CUT",
            poly_condition_id="0x123abc",
            kalshi_question="Will the Fed cut rates?",
            poly_question="Fed rate cut May 2026?",
            similarity=0.92,
            validated=True,
        )
        pairs = tmp_db.get_cross_platform_pairs()
        assert len(pairs) == 1
        pair = pairs[0]
        assert pair["kalshi_ticker"] == "FED-RATE-CUT"
        assert pair["poly_condition_id"] == "0x123abc"
        assert pair["similarity"] == pytest.approx(0.92, abs=0.01)
        assert pair["validated"] == 1
        assert pair["validated_at"] is not None

    def test_upsert_updates_on_conflict(self, tmp_db):
        tmp_db.upsert_cross_platform_pair(
            kalshi_ticker="FED-CUT",
            poly_condition_id="0xABC",
            similarity=0.80,
            validated=False,
        )
        tmp_db.upsert_cross_platform_pair(
            kalshi_ticker="FED-CUT",
            poly_condition_id="0xABC",
            similarity=0.95,
            validated=True,
        )
        pairs = tmp_db.get_cross_platform_pairs()
        assert len(pairs) == 1
        assert pairs[0]["similarity"] == pytest.approx(0.95, abs=0.01)
        assert pairs[0]["validated"] == 1

    def test_validated_only_filter(self, tmp_db):
        tmp_db.upsert_cross_platform_pair("T1", "P1", similarity=0.90, validated=True)
        tmp_db.upsert_cross_platform_pair("T2", "P2", similarity=0.85, validated=False)
        tmp_db.upsert_cross_platform_pair("T3", "P3", similarity=0.80, validated=True)

        all_pairs = tmp_db.get_cross_platform_pairs(validated_only=False)
        assert len(all_pairs) == 3

        validated = tmp_db.get_cross_platform_pairs(validated_only=True)
        assert len(validated) == 2
        tickers = {p["kalshi_ticker"] for p in validated}
        assert tickers == {"T1", "T3"}

    def test_ordered_by_similarity_desc(self, tmp_db):
        tmp_db.upsert_cross_platform_pair("LOW", "P1", similarity=0.50, validated=True)
        tmp_db.upsert_cross_platform_pair("HIGH", "P2", similarity=0.99, validated=True)
        tmp_db.upsert_cross_platform_pair("MID", "P3", similarity=0.75, validated=True)
        pairs = tmp_db.get_cross_platform_pairs()
        assert pairs[0]["kalshi_ticker"] == "HIGH"
        assert pairs[1]["kalshi_ticker"] == "MID"
        assert pairs[2]["kalshi_ticker"] == "LOW"

    def test_empty_pairs(self, tmp_db):
        pairs = tmp_db.get_cross_platform_pairs()
        assert pairs == []

    def test_validated_at_none_when_not_validated(self, tmp_db):
        tmp_db.upsert_cross_platform_pair("T1", "P1", similarity=0.80, validated=False)
        pairs = tmp_db.get_cross_platform_pairs()
        assert pairs[0]["validated_at"] is None
