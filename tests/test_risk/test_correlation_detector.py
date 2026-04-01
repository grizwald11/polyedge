"""Tests for correlation detector module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.models import (
    Direction,
    Market,
    MarketCategory,
    MarketToken,
    Side,
    StrategyName,
    Trade,
)
from src.execution.position_manager import PositionManager
from src.risk.correlation_detector import CorrelationDetector, _DEFAULT_KEYWORD_OVERLAP_THRESHOLD


# ──────────────────────────────────────
# Helpers
# ──────────────────────────────────────

def _make_market(
    ticker: str,
    question: str = "",
    event_ticker: str = "",
    category: str = "Politics",
) -> Market:
    if not question:
        question = f"Test market {ticker}"
    cat = MarketCategory(category) if category in [e.value for e in MarketCategory] else MarketCategory.OTHER
    return Market(
        ticker=ticker,
        question=question,
        category=cat,
        event_ticker=event_ticker,
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=0.40),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=0.60),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
        volume_24h=100000,
    )


def _make_trade(
    market_id: str,
    token_id: str = "",
    price: float = 0.40,
    size: float = 10,
) -> Trade:
    if not token_id:
        token_id = f"{market_id}_yes"
    return Trade(
        order_id=f"PE-{market_id}",
        market_id=market_id,
        token_id=token_id,
        side=Side.BUY,
        price=price,
        size=size,
        fee=0.0,
        strategy=StrategyName.AI_PROBABILITY,
        paper=True,
    )


class _FakeSettings:
    """Minimal settings stub for tests."""

    class trading:
        max_correlated_exposure_pct = 0.20


# ──────────────────────────────────────
# Tests
# ──────────────────────────────────────

class TestSameEventTicker:
    """Same event_ticker should be treated as 100% correlated."""

    def test_same_event_blocks_when_over_limit(self, tmp_db):
        """Trade in same event that would breach 20% limit is rejected."""
        m1 = _make_market("MKT-A", event_ticker="EVENT-1")
        m2 = _make_market("MKT-B", event_ticker="EVENT-1")
        tmp_db.upsert_market(m1)
        tmp_db.upsert_market(m2)

        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade("MKT-A", price=0.40, size=50))  # cost_basis = $20

        detector = CorrelationDetector(pm, tmp_db, _FakeSettings)
        # bankroll=100 → max_allowed = $20. Existing $20 + proposed $10 = $30 > $20
        result = detector.check_correlation("MKT-B", proposed_size_dollars=10.0, bankroll=100.0)

        assert not result.allowed
        assert result.correlated_exposure == pytest.approx(30.0)
        assert result.max_allowed == pytest.approx(20.0)
        assert len(result.correlations) == 1
        assert "same_event_ticker" in result.correlations[0]["reason"]

    def test_same_event_allows_when_under_limit(self, tmp_db):
        """Trade in same event that stays under 20% limit is allowed."""
        m1 = _make_market("MKT-A", event_ticker="EVENT-1")
        m2 = _make_market("MKT-B", event_ticker="EVENT-1")
        tmp_db.upsert_market(m1)
        tmp_db.upsert_market(m2)

        pm = PositionManager(tmp_db, bankroll=1000.0)
        pm.update_from_trade(_make_trade("MKT-A", price=0.10, size=10))  # cost_basis = $1

        detector = CorrelationDetector(pm, tmp_db, _FakeSettings)
        # bankroll=1000 → max_allowed = $200. $1 + $5 = $6 < $200
        result = detector.check_correlation("MKT-B", proposed_size_dollars=5.0, bankroll=1000.0)

        assert result.allowed
        assert result.correlated_exposure == pytest.approx(6.0)


class TestDifferentEvents:
    """Different event tickers should not trigger event-ticker correlation."""

    def test_different_events_allowed(self, tmp_db):
        """Markets in unrelated events are not correlated."""
        m1 = _make_market("MKT-A", event_ticker="EVENT-1")
        m2 = _make_market("MKT-B", event_ticker="EVENT-2",
                          question="Completely different topic about sports")
        tmp_db.upsert_market(m1)
        tmp_db.upsert_market(m2)

        pm = PositionManager(tmp_db, bankroll=500.0)
        pm.update_from_trade(_make_trade("MKT-A", price=0.50, size=100))  # $50

        detector = CorrelationDetector(pm, tmp_db, _FakeSettings)
        # No correlation, so correlated_exposure = just the proposed $10
        result = detector.check_correlation("MKT-B", proposed_size_dollars=10.0, bankroll=500.0)

        assert result.allowed
        assert result.correlated_exposure == pytest.approx(10.0)
        assert len(result.correlations) == 0


class TestKeywordOverlap:
    """Category + keyword overlap should trigger partial correlation."""

    def test_high_keyword_overlap_partially_correlated(self, tmp_db):
        """Markets in same category with similar questions are 50% correlated."""
        m1 = _make_market(
            "TRUMP-WIN",
            question="Will Trump win the 2028 presidential election in November?",
            event_ticker="ELECTION-2028",
            category="Politics",
        )
        m2 = _make_market(
            "TRUMP-NOMINEE",
            question="Will Trump win the 2028 presidential election popular vote?",
            event_ticker="PRIMARY-2028",
            category="Politics",
        )
        tmp_db.upsert_market(m1)
        tmp_db.upsert_market(m2)

        pm = PositionManager(tmp_db, bankroll=1000.0)
        pm.update_from_trade(_make_trade("TRUMP-WIN", price=0.50, size=40))  # $20

        detector = CorrelationDetector(pm, tmp_db, _FakeSettings)
        result = detector.check_correlation("TRUMP-NOMINEE", proposed_size_dollars=10.0, bankroll=1000.0)

        # 50% of $20 = $10 keyword correlation + $10 proposed = $20
        assert result.allowed  # $20 < $200 max
        assert len(result.correlations) == 1
        assert result.correlations[0]["weight"] == pytest.approx(0.50)
        assert "keyword_overlap" in result.correlations[0]["reason"]

    def test_low_keyword_overlap_not_correlated(self, tmp_db):
        """Markets in same category but different topics are not keyword-correlated."""
        m1 = _make_market(
            "FED-RATE",
            question="Will the Federal Reserve cut interest rates in May 2026?",
            event_ticker="FED-MAY",
            category="Fed/Macro",
        )
        m2 = _make_market(
            "CPI-JUNE",
            question="Will June 2026 CPI inflation come in below 3 percent?",
            event_ticker="CPI-JUNE-2026",
            category="Fed/Macro",
        )
        tmp_db.upsert_market(m1)
        tmp_db.upsert_market(m2)

        pm = PositionManager(tmp_db, bankroll=1000.0)
        pm.update_from_trade(_make_trade("FED-RATE", price=0.40, size=50))  # $20

        detector = CorrelationDetector(pm, tmp_db, _FakeSettings)
        result = detector.check_correlation("CPI-JUNE", proposed_size_dollars=10.0, bankroll=1000.0)

        # Different topics, low overlap → no keyword correlation
        assert result.allowed
        assert result.correlated_exposure == pytest.approx(10.0)
        assert len(result.correlations) == 0

    def test_different_category_no_keyword_correlation(self, tmp_db):
        """Markets with overlapping keywords but different categories are not correlated."""
        m1 = _make_market(
            "TRUMP-POLITICS",
            question="Will Trump win the 2028 election?",
            event_ticker="ELECTION-2028",
            category="Politics",
        )
        m2 = _make_market(
            "TRUMP-CULTURE",
            question="Will Trump win a Grammy award in 2028?",
            event_ticker="GRAMMYS-2028",
            category="Culture",
        )
        tmp_db.upsert_market(m1)
        tmp_db.upsert_market(m2)

        pm = PositionManager(tmp_db, bankroll=1000.0)
        pm.update_from_trade(_make_trade("TRUMP-POLITICS", price=0.50, size=40))

        detector = CorrelationDetector(pm, tmp_db, _FakeSettings)
        result = detector.check_correlation("TRUMP-CULTURE", proposed_size_dollars=10.0, bankroll=1000.0)

        assert result.allowed
        assert len(result.correlations) == 0


class TestExposureLimits:
    """Exposure accumulation and limit enforcement."""

    def test_multiple_correlated_positions_accumulate(self, tmp_db):
        """Multiple positions in the same event all count toward correlated exposure."""
        for i in range(4):
            m = _make_market(f"MKT-{i}", event_ticker="BIG-EVENT")
            tmp_db.upsert_market(m)

        proposed = _make_market("MKT-NEW", event_ticker="BIG-EVENT")
        tmp_db.upsert_market(proposed)

        pm = PositionManager(tmp_db, bankroll=1000.0)
        for i in range(4):
            pm.update_from_trade(_make_trade(f"MKT-{i}", price=0.50, size=20))  # $10 each

        detector = CorrelationDetector(pm, tmp_db, _FakeSettings)
        # 4 positions * $10 = $40 correlated + $15 proposed = $55
        # bankroll=200 → max=$40
        result = detector.check_correlation("MKT-NEW", proposed_size_dollars=15.0, bankroll=200.0)

        assert not result.allowed
        assert result.correlated_exposure == pytest.approx(55.0)
        assert len(result.correlations) == 4

    def test_mixed_event_and_keyword_correlation(self, tmp_db):
        """Event-ticker and keyword correlations both contribute to total."""
        m_same_event = _make_market(
            "RATE-CUT-A",
            question="Will the Fed cut rates in May?",
            event_ticker="FED-MAY",
            category="Fed/Macro",
        )
        m_keyword = _make_market(
            "RATE-CUT-B",
            question="Will the Fed cut rates in June?",
            event_ticker="FED-JUNE",
            category="Fed/Macro",
        )
        m_proposed = _make_market(
            "RATE-CUT-C",
            question="Will the Fed cut rates in July?",
            event_ticker="FED-MAY",  # Same event as A
            category="Fed/Macro",
        )
        tmp_db.upsert_market(m_same_event)
        tmp_db.upsert_market(m_keyword)
        tmp_db.upsert_market(m_proposed)

        pm = PositionManager(tmp_db, bankroll=1000.0)
        pm.update_from_trade(_make_trade("RATE-CUT-A", price=0.50, size=20))  # $10
        pm.update_from_trade(_make_trade("RATE-CUT-B", price=0.50, size=20))  # $10

        detector = CorrelationDetector(pm, tmp_db, _FakeSettings)
        result = detector.check_correlation("RATE-CUT-C", proposed_size_dollars=5.0, bankroll=1000.0)

        # A: same event → $10 * 1.0 = $10
        # B: keyword overlap → $10 * 0.5 = $5
        # Total: $10 + $5 + $5 proposed = $20
        assert result.allowed  # $20 < $200
        assert len(result.correlations) == 2


class TestEmptyPortfolio:
    """An empty portfolio should always allow trades (within bankroll limits)."""

    def test_empty_portfolio_allows_trade(self, tmp_db):
        m = _make_market("NEW-MKT", event_ticker="NEW-EVENT")
        tmp_db.upsert_market(m)

        pm = PositionManager(tmp_db, bankroll=500.0)
        detector = CorrelationDetector(pm, tmp_db, _FakeSettings)

        result = detector.check_correlation("NEW-MKT", proposed_size_dollars=50.0, bankroll=500.0)

        assert result.allowed
        assert result.correlated_exposure == pytest.approx(50.0)
        assert result.max_allowed == pytest.approx(100.0)
        assert len(result.correlations) == 0

    def test_empty_portfolio_rejects_oversized_trade(self, tmp_db):
        """Even with no existing positions, a trade exceeding the limit is rejected."""
        m = _make_market("BIG-MKT")
        tmp_db.upsert_market(m)

        pm = PositionManager(tmp_db, bankroll=100.0)
        detector = CorrelationDetector(pm, tmp_db, _FakeSettings)

        # Proposed $25 > max_allowed $20 (20% of $100)
        result = detector.check_correlation("BIG-MKT", proposed_size_dollars=25.0, bankroll=100.0)

        assert not result.allowed
        assert result.correlated_exposure == pytest.approx(25.0)
        assert result.max_allowed == pytest.approx(20.0)


class TestKeywordOverlapMethod:
    """Unit tests for the _keyword_overlap static method."""

    def test_identical_questions(self):
        overlap = CorrelationDetector._keyword_overlap(
            "Will Trump win the 2028 election?",
            "Will Trump win the 2028 election?",
        )
        assert overlap == pytest.approx(1.0)

    def test_completely_different_questions(self):
        overlap = CorrelationDetector._keyword_overlap(
            "Will Bitcoin reach 100000 dollars?",
            "Will aliens contact Earth tomorrow?",
        )
        assert overlap == pytest.approx(0.0)

    def test_partial_overlap(self):
        overlap = CorrelationDetector._keyword_overlap(
            "Will Trump win the 2028 presidential election?",
            "Will Trump lose the 2028 Republican primary?",
        )
        # Common: trump, 2028. Union: trump, 2028, presidential, election, lose, republican, primary
        assert 0.0 < overlap < 1.0

    def test_empty_strings(self):
        assert CorrelationDetector._keyword_overlap("", "") == pytest.approx(0.0)
        assert CorrelationDetector._keyword_overlap("test question", "") == pytest.approx(0.0)


class TestEventTickerNormalization:
    """Platform prefixes on event tickers should be stripped for matching."""

    def test_cross_platform_event_match(self, tmp_db):
        m1 = _make_market("MKT-KALSHI", event_ticker="KALSHI:TRUMP-WINS")
        m2 = _make_market("MKT-POLY", event_ticker="POLY:TRUMP-WINS")
        tmp_db.upsert_market(m1)
        tmp_db.upsert_market(m2)

        pm = PositionManager(tmp_db, bankroll=1000.0)
        pm.update_from_trade(_make_trade("MKT-KALSHI", price=0.50, size=20))  # $10

        detector = CorrelationDetector(pm, tmp_db, _FakeSettings)
        result = detector.check_correlation("MKT-POLY", proposed_size_dollars=5.0, bankroll=1000.0)

        assert len(result.correlations) == 1
        assert "same_event_ticker" in result.correlations[0]["reason"]


class TestCacheClearing:
    """Cache should be clearable for fresh scan cycles."""

    def test_clear_cache(self, tmp_db):
        pm = PositionManager(tmp_db, bankroll=500.0)
        detector = CorrelationDetector(pm, tmp_db, _FakeSettings)

        # Prime cache
        detector._get_market_data("NONEXISTENT")
        assert "NONEXISTENT" in detector._market_cache

        detector.clear_cache()
        assert len(detector._market_cache) == 0
