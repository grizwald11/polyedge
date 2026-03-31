"""C-2: Verify Kalshi side/action parameter mapping for all 4 combinations.

Ensures order_builder correctly maps Direction → (Side, kalshi_side) and that
order_router passes these to the Kalshi API correctly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.core.models import (
    Direction,
    Market,
    MarketCategory,
    MarketToken,
    Order,
    Side,
    Signal,
    StrategyName,
)
from src.execution.order_builder import OrderBuilder


@pytest.fixture
def builder() -> OrderBuilder:
    return OrderBuilder(Settings(trading=Settings.model_fields["trading"].default_factory()))


@pytest.fixture
def market() -> Market:
    return Market(
        ticker="SIDE-TEST",
        question="Side mapping test?",
        category=MarketCategory.POLITICS,
        tags=["Politics"],
        tokens=[
            MarketToken(token_id="SIDE-TEST_yes", outcome="Yes", price=0.60),
            MarketToken(token_id="SIDE-TEST_no", outcome="No", price=0.40),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
        volume_24h=50000.0,
        liquidity=10000.0,
        active=True,
    )


def _make_signal(direction: Direction) -> Signal:
    return Signal(
        strategy=StrategyName.AI_PROBABILITY,
        market_id="SIDE-TEST",
        market_question="Side mapping test?",
        direction=direction,
        edge=0.10,
        probability_estimate=0.50,
        market_price=0.50,
        confidence=0.70,
        reasoning="Test",
    )


class TestSideActionMapping:
    """C-2: Verify all 4 Direction → (Side, kalshi_side) combinations."""

    def test_buy_yes(self, builder, market):
        signal = _make_signal(Direction.BUY_YES)
        order = builder.build_limit_order(market, signal, size=5, price=0.60)
        assert order is not None
        assert order.side == Side.BUY
        assert order.kalshi_side == "yes"
        assert order.token_id == "SIDE-TEST_yes"

    def test_buy_no(self, builder, market):
        signal = _make_signal(Direction.BUY_NO)
        order = builder.build_limit_order(market, signal, size=5, price=0.40)
        assert order is not None
        assert order.side == Side.BUY
        assert order.kalshi_side == "no"
        assert order.token_id == "SIDE-TEST_no"

    def test_sell_yes(self, builder, market):
        signal = _make_signal(Direction.SELL_YES)
        order = builder.build_limit_order(market, signal, size=5, price=0.60)
        assert order is not None
        assert order.side == Side.SELL
        assert order.kalshi_side == "yes"
        assert order.token_id == "SIDE-TEST_yes"

    def test_sell_no(self, builder, market):
        signal = _make_signal(Direction.SELL_NO)
        order = builder.build_limit_order(market, signal, size=5, price=0.40)
        assert order is not None
        assert order.side == Side.SELL
        assert order.kalshi_side == "no"
        assert order.token_id == "SIDE-TEST_no"

    def test_action_values_match_side(self, builder, market):
        """Verify order.side.value.lower() produces correct Kalshi action string."""
        for direction, expected_action in [
            (Direction.BUY_YES, "buy"),
            (Direction.BUY_NO, "buy"),
            (Direction.SELL_YES, "sell"),
            (Direction.SELL_NO, "sell"),
        ]:
            signal = _make_signal(direction)
            order = builder.build_limit_order(market, signal, size=5, price=0.50)
            assert order is not None
            # This is what order_router passes as `action=` to Kalshi API
            assert order.side.value.lower() == expected_action, \
                f"Direction {direction.value}: expected action '{expected_action}', got '{order.side.value.lower()}'"

    def test_kalshi_side_values(self, builder, market):
        """Verify kalshi_side produces correct Kalshi side string."""
        for direction, expected_side in [
            (Direction.BUY_YES, "yes"),
            (Direction.BUY_NO, "no"),
            (Direction.SELL_YES, "yes"),
            (Direction.SELL_NO, "no"),
        ]:
            signal = _make_signal(direction)
            order = builder.build_limit_order(market, signal, size=5, price=0.50)
            assert order is not None
            # This is what order_router passes as `side=` to Kalshi API
            assert order.kalshi_side == expected_side, \
                f"Direction {direction.value}: expected side '{expected_side}', got '{order.kalshi_side}'"
