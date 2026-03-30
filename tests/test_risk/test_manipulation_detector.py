"""Tests for market manipulation detector."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.core.models import Market, MarketToken
from src.risk.manipulation_detector import (
    ManipulationDetector,
    ManipulationFlag,
    RAPID_MOVE_THRESHOLD,
)


@pytest.fixture
def detector() -> ManipulationDetector:
    return ManipulationDetector()


def _make_market(
    ticker: str = "TEST-MKT",
    yes_price: float = 0.50,
    no_price: float = 0.50,
) -> Market:
    return Market(
        ticker=ticker,
        question="Test market",
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=yes_price),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=no_price),
        ],
    )


class TestRapidMoveDetection:
    def test_no_flag_on_normal_move(self, detector):
        """Small price move should not flag."""
        market1 = _make_market(yes_price=0.50, no_price=0.50)
        detector.check_market(market1)

        market2 = _make_market(yes_price=0.55, no_price=0.45)
        flag = detector.check_market(market2)
        assert flag is None

    def test_flags_rapid_move(self, detector):
        """Large price move (>20%) should flag."""
        market1 = _make_market(yes_price=0.50, no_price=0.50)
        detector.check_market(market1)

        market2 = _make_market(yes_price=0.75, no_price=0.25)
        flag = detector.check_market(market2)
        assert flag is not None
        assert "Rapid price move" in flag.reason
        assert flag.market_id == "TEST-MKT"
        assert flag.price_move == pytest.approx(0.25, abs=0.01)

    def test_flagged_market_stays_flagged(self, detector):
        """Once flagged, subsequent checks return the same flag."""
        market1 = _make_market(yes_price=0.50, no_price=0.50)
        detector.check_market(market1)

        market2 = _make_market(yes_price=0.75, no_price=0.25)
        flag1 = detector.check_market(market2)
        assert flag1 is not None

        # Even with a normal price, still flagged
        market3 = _make_market(yes_price=0.76, no_price=0.24)
        flag2 = detector.check_market(market3)
        assert flag2 is not None
        assert flag2.market_id == flag1.market_id

    def test_flag_expires(self, detector):
        """Flag should expire after expiry period."""
        detector.flag_expiry_seconds = 0.1  # Very short for testing

        market1 = _make_market(yes_price=0.50, no_price=0.50)
        detector.check_market(market1)

        market2 = _make_market(yes_price=0.75, no_price=0.25)
        flag = detector.check_market(market2)
        assert flag is not None

        time.sleep(0.15)

        # After expiry, flag should be gone
        market3 = _make_market(yes_price=0.76, no_price=0.24)
        flag = detector.check_market(market3)
        assert flag is None

    def test_no_flag_on_first_observation(self, detector):
        """First observation for a market should never flag."""
        market = _make_market(yes_price=0.50, no_price=0.50)
        flag = detector.check_market(market)
        assert flag is None

    def test_different_markets_independent(self, detector):
        """Flags on one market don't affect another."""
        mkt_a1 = _make_market(ticker="MKT-A", yes_price=0.50, no_price=0.50)
        detector.check_market(mkt_a1)

        mkt_a2 = _make_market(ticker="MKT-A", yes_price=0.75, no_price=0.25)
        flag_a = detector.check_market(mkt_a2)
        assert flag_a is not None

        mkt_b = _make_market(ticker="MKT-B", yes_price=0.50, no_price=0.50)
        flag_b = detector.check_market(mkt_b)
        assert flag_b is None

    def test_custom_threshold(self):
        """Custom rapid move threshold is respected."""
        detector = ManipulationDetector(rapid_move_threshold=0.10)

        market1 = _make_market(yes_price=0.50, no_price=0.50)
        detector.check_market(market1)

        market2 = _make_market(yes_price=0.62, no_price=0.38)
        flag = detector.check_market(market2)
        assert flag is not None


class TestCrossedBookDetection:
    def test_no_flag_on_normal_book(self, detector):
        """YES + NO ~= 1.0 should not flag."""
        market = _make_market(yes_price=0.50, no_price=0.50)
        detector.check_market(market)  # First observation
        flag = detector.check_market(market)
        assert flag is None

    def test_flags_crossed_book(self, detector):
        """YES + NO significantly != 1.0 should flag."""
        market = _make_market(yes_price=0.60, no_price=0.55)  # Sum = 1.15
        detector.check_market(market)  # First observation
        flag = detector.check_market(market)
        assert flag is not None
        assert "Crossed/inverted book" in flag.reason

    def test_minor_deviation_ok(self, detector):
        """Small deviations (within 8%) should not flag."""
        market = _make_market(yes_price=0.52, no_price=0.52)  # Sum = 1.04
        detector.check_market(market)  # First observation
        flag = detector.check_market(market)
        assert flag is None


class TestIsFlag:
    def test_is_flagged(self, detector):
        market1 = _make_market(yes_price=0.50, no_price=0.50)
        detector.check_market(market1)
        assert not detector.is_flagged("TEST-MKT")

        market2 = _make_market(yes_price=0.75, no_price=0.25)
        detector.check_market(market2)
        assert detector.is_flagged("TEST-MKT")

    def test_clear_flag(self, detector):
        market1 = _make_market(yes_price=0.50, no_price=0.50)
        detector.check_market(market1)
        market2 = _make_market(yes_price=0.75, no_price=0.25)
        detector.check_market(market2)

        detector.clear_flag("TEST-MKT")
        assert not detector.is_flagged("TEST-MKT")

    def test_history_trimmed(self, detector):
        """Price history should not grow unbounded."""
        detector._max_history = 5
        for i in range(20):
            price = 0.40 + (i * 0.01)
            detector.update_price("TEST-MKT", price)
        assert len(detector._price_history["TEST-MKT"]) == 5


class TestRiskEngineIntegration:
    """Test that manipulation detector integrates with risk engine."""

    def test_manipulation_flag_blocks_trade(self, settings, tmp_db):
        from src.core.models import Direction, Signal, StrategyName
        from src.execution.position_manager import PositionManager
        from src.risk.circuit_breaker import CircuitBreaker
        from src.risk.risk_engine import RiskEngine
        from datetime import datetime, timedelta, timezone

        pm = PositionManager(tmp_db, bankroll=500.0)
        cb = CircuitBreaker(settings, tmp_db)
        md = ManipulationDetector()
        engine = RiskEngine(settings, pm, cb, tmp_db, manipulation_detector=md)

        # Seed the detector by checking a market at one price, then at a big jump
        market_before = Market(
            ticker="FED-CUT",
            question="Will the Fed cut?",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.30),
                MarketToken(token_id="no", outcome="No", price=0.70),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
            liquidity=50000,
        )
        md.check_market(market_before)  # Records 0.30

        market = Market(
            ticker="FED-CUT",
            question="Will the Fed cut?",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.60),
                MarketToken(token_id="no", outcome="No", price=0.40),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
            liquidity=50000,
        )
        # check_market records 0.60 and detects 0.30->0.60 = 30% move
        md.check_market(market)

        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="FED-CUT",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.70,
            market_price=0.60,
            confidence=0.7,
        )

        result = engine.check_all(signal, market, proposed_size=5, proposed_cost=3.00)
        assert result.passed is False
        assert any("Manipulation flag" in c for c in result.failed_checks)

    def test_clean_market_passes_manipulation_check(self, settings, tmp_db):
        from src.core.models import Direction, Signal, StrategyName
        from src.execution.position_manager import PositionManager
        from src.risk.circuit_breaker import CircuitBreaker
        from src.risk.risk_engine import RiskEngine
        from datetime import datetime, timedelta, timezone

        pm = PositionManager(tmp_db, bankroll=500.0)
        cb = CircuitBreaker(settings, tmp_db)
        md = ManipulationDetector()
        engine = RiskEngine(settings, pm, cb, tmp_db, manipulation_detector=md)

        # Seed with normal price movement
        md.update_price("FED-CUT", 0.30)
        md.update_price("FED-CUT", 0.34)  # Small move -> clean

        market = Market(
            ticker="FED-CUT",
            question="Will the Fed cut?",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
            liquidity=50000,
        )
        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="FED-CUT",
            direction=Direction.BUY_YES,
            edge=0.08,
            probability_estimate=0.42,
            market_price=0.34,
            confidence=0.7,
        )

        result = engine.check_all(signal, market, proposed_size=5, proposed_cost=1.70)
        assert not any("Manipulation" in c for c in result.failed_checks)
