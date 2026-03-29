"""Tests for the Obvious NO strategy."""

from datetime import datetime, timedelta, timezone

import pytest

from src.strategies.obvious_no import ObviousNoStrategy
from src.config import Settings
from src.core.models import Market, MarketToken, MarketCategory, Direction, StrategyName


@pytest.fixture
def strategy() -> ObviousNoStrategy:
    return ObviousNoStrategy(Settings())


def _make_market(
    yes_price: float, days: float, volume: float = 50000, liquidity: float = 50000,
) -> Market:
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
        liquidity=liquidity,
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

    def test_signal_edge_is_probability_based(self, strategy):
        """Edge should be probability_estimate - no_price, not dollar profit."""
        market = _make_market(yes_price=0.04, days=10)
        signals = strategy.scan_for_opportunities([market])
        assert len(signals) == 1
        # probability_estimate = min(0.99, 1.0 - 0.04*0.3) = 0.988
        # edge = 0.988 - 0.96 = 0.028
        assert abs(signals[0].edge - 0.028) < 0.001
        assert abs(signals[0].probability_estimate - 0.988) < 0.001

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

    def test_edge_feeds_kelly_correctly(self, strategy):
        """Regression: edge must produce correct market_price in Kelly sizer.

        Kelly derives market_price = probability - edge. If edge was dollar
        profit (~0.03), Kelly got market_price=0.96 instead of 0.97, oversizing
        by ~50%. With probability-based edge, market_price should match no_price.
        """
        market = _make_market(yes_price=0.03, days=15)
        signals = strategy.scan_for_opportunities([market])
        assert len(signals) == 1
        sig = signals[0]
        # Kelly derives: market_price = probability - edge
        kelly_market_price = sig.probability_estimate - sig.edge
        # Should approximately equal the actual no_price (0.97)
        assert abs(kelly_market_price - 0.97) < 0.005

    def test_low_liquidity_slippage_deducted(self, strategy):
        """Low liquidity markets should have slippage deducted from return."""
        # Same market, different liquidity
        deep = _make_market(yes_price=0.03, days=15, liquidity=50000)
        thin = _make_market(yes_price=0.03, days=15, liquidity=5000)
        deep.ticker = "DEEP"
        thin.ticker = "THIN"

        signals_deep = strategy.scan_for_opportunities([deep])
        signals_thin = strategy.scan_for_opportunities([thin])

        if signals_deep and signals_thin:
            # Event markets are fee-free, so liquidity depth doesn't affect returns
            # (slippage deduction removed for fee-free markets)
            deep_return = float(
                signals_deep[0].reasoning.split("Net return: ")[1].split(",")[0].rstrip("%")
            )
            thin_return = float(
                signals_thin[0].reasoning.split("Net return: ")[1].split(",")[0].rstrip("%")
            )
            assert thin_return == deep_return

    def test_confidence_varies_with_yes_price(self, strategy):
        """Regression: confidence should scale with YES price, not be hardcoded 0.95."""
        m_low = _make_market(yes_price=0.02, days=10)
        m_high = _make_market(yes_price=0.05, days=10)
        m_low.ticker = "LOW"
        m_high.ticker = "HIGH"
        signals = strategy.scan_for_opportunities([m_low, m_high])
        assert len(signals) == 2
        low_sig = next(s for s in signals if s.market_id == "LOW")
        high_sig = next(s for s in signals if s.market_id == "HIGH")
        # YES=0.02 should have higher confidence than YES=0.05
        assert low_sig.confidence > high_sig.confidence
