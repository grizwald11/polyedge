"""Tests for the Obvious NO strategy."""

from datetime import datetime, timedelta, timezone

import pytest

from src.strategies.obvious_no import ObviousNoStrategy
from src.config import Settings
from src.core.models import Market, MarketToken, MarketCategory, Direction, StrategyName


@pytest.fixture
def strategy() -> ObviousNoStrategy:
    return ObviousNoStrategy(Settings())


def _make_market(yes_price: float, days: float, volume: float = 50000) -> Market:
    return Market(
        ticker="TEST-OBVIOUS",
        question="Will an absurd thing happen?",
        category=MarketCategory.OTHER,
        tokens=[
            MarketToken(token_id="TEST-OBVIOUS_yes", outcome="Yes", price=yes_price),
            MarketToken(token_id="TEST-OBVIOUS_no", outcome="No", price=1.0 - yes_price),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=days),
        volume_24h=volume,
        active=True,
    )


class TestObviousNoStrategy:
    def test_qualifies_low_yes_price(self, strategy):
        market = _make_market(yes_price=0.03, days=15)
        signals = strategy.scan_for_opportunities([market])
        assert len(signals) == 1
        assert signals[0].direction == Direction.BUY_NO
        assert signals[0].strategy == StrategyName.OBVIOUS_NO

    def test_rejects_high_yes_price(self, strategy):
        market = _make_market(yes_price=0.10, days=15)
        signals = strategy.scan_for_opportunities([market])
        assert len(signals) == 0

    def test_rejects_too_far_resolution(self, strategy):
        market = _make_market(yes_price=0.03, days=60)
        signals = strategy.scan_for_opportunities([market])
        assert len(signals) == 0

    def test_rejects_no_resolution_date(self, strategy):
        market = Market(
            ticker="NO-DATE",
            question="No end date market",
            tokens=[
                MarketToken(token_id="NO-DATE_yes", outcome="Yes", price=0.02),
                MarketToken(token_id="NO-DATE_no", outcome="No", price=0.98),
            ],
            volume_24h=50000,
            active=True,
        )
        signals = strategy.scan_for_opportunities([market])
        assert len(signals) == 0

    def test_calculates_return(self, strategy):
        market = _make_market(yes_price=0.03, days=15)
        signals = strategy.scan_for_opportunities([market])
        assert len(signals) == 1
        # Simple return: (1.0 - 0.97) / 0.97 ≈ 3.09%
        assert "return" in signals[0].reasoning.lower()

    def test_signal_edge_is_yes_price(self, strategy):
        market = _make_market(yes_price=0.04, days=10)
        signals = strategy.scan_for_opportunities([market])
        assert len(signals) == 1
        assert abs(signals[0].edge - 0.04) < 0.001

    def test_rejects_yes_price_too_low(self, strategy):
        # YES at $0.00 — no token to buy NO against
        market = Market(
            ticker="ZERO",
            question="Zero price",
            tokens=[
                MarketToken(token_id="ZERO_yes", outcome="Yes", price=0.0),
                MarketToken(token_id="ZERO_no", outcome="No", price=1.0),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=10),
            volume_24h=50000,
            active=True,
        )
        signals = strategy.scan_for_opportunities([market])
        assert len(signals) == 0

    def test_multiple_markets(self, strategy):
        m1 = _make_market(yes_price=0.02, days=10)
        m2 = _make_market(yes_price=0.04, days=20)
        m3 = _make_market(yes_price=0.50, days=10)  # Should not qualify

        # Need unique tickers
        m1.ticker = "M1"
        m2.ticker = "M2"
        m3.ticker = "M3"

        signals = strategy.scan_for_opportunities([m1, m2, m3])
        assert len(signals) == 2
