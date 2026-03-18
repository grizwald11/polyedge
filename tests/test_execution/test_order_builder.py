"""Tests for order builder."""

from __future__ import annotations

import pytest

from src.config import Settings
from src.core.models import (
    Direction, Market, MarketCategory, MarketToken, OrderType, OrderStatus,
    Side, Signal, StrategyName,
)
from src.execution.order_builder import OrderBuilder


@pytest.fixture
def builder(settings) -> OrderBuilder:
    return OrderBuilder(settings)


@pytest.fixture
def signal_buy_yes(sample_market) -> Signal:
    return Signal(
        strategy=StrategyName.AI_PROBABILITY,
        market_id=sample_market.ticker,
        direction=Direction.BUY_YES,
        edge=0.08,
        probability_estimate=0.42,
        market_price=0.34,
        confidence=0.7,
    )


@pytest.fixture
def signal_buy_no(sample_market) -> Signal:
    return Signal(
        strategy=StrategyName.OBVIOUS_NO,
        market_id=sample_market.ticker,
        direction=Direction.BUY_NO,
        edge=0.03,
        probability_estimate=0.97,
        market_price=0.66,
        confidence=0.95,
    )


class TestBuildLimitOrder:
    def test_basic_limit_order(self, builder, sample_market, signal_buy_yes):
        order = builder.build_limit_order(sample_market, signal_buy_yes, size=10, price=0.34)

        assert order.market_id == "FED-RATE-CUT-MAY26"
        assert order.side == Side.BUY
        assert order.token_id == "FED-RATE-CUT-MAY26_yes"
        assert order.price == 0.34
        assert order.size == 10
        # cost = price * size + maker_fee = 3.40 + 0.01 = 3.41
        assert order.cost == pytest.approx(3.41)
        assert order.order_type == OrderType.GTC
        assert order.status == OrderStatus.PENDING
        assert order.strategy == StrategyName.AI_PROBABILITY
        assert order.paper is True
        assert order.id.startswith("PE-")

    def test_buy_no_limit_order(self, builder, sample_market, signal_buy_no):
        order = builder.build_limit_order(sample_market, signal_buy_no, size=5, price=0.66)

        assert order.side == Side.BUY
        assert order.token_id == "FED-RATE-CUT-MAY26_no"
        assert order.price == 0.66
        assert order.size == 5

    def test_price_clamping(self, builder, sample_market, signal_buy_yes):
        order = builder.build_limit_order(sample_market, signal_buy_yes, size=1, price=1.50)
        assert order.price == 0.99

        order2 = builder.build_limit_order(sample_market, signal_buy_yes, size=1, price=-0.10)
        assert order2.price == 0.01

    def test_unique_order_ids(self, builder, sample_market, signal_buy_yes):
        o1 = builder.build_limit_order(sample_market, signal_buy_yes, 1, 0.34)
        o2 = builder.build_limit_order(sample_market, signal_buy_yes, 1, 0.34)
        assert o1.id != o2.id

    def test_maker_fee_rate(self, builder, sample_market, signal_buy_yes):
        order = builder.build_limit_order(sample_market, signal_buy_yes, 10, 0.34)
        assert order.fee_rate_bps == 175


class TestBuildMarketOrder:
    def test_basic_market_order(self, builder, sample_market, signal_buy_yes):
        order = builder.build_market_order(sample_market, signal_buy_yes, size=10)

        assert order.market_id == "FED-RATE-CUT-MAY26"
        assert order.order_type == OrderType.FOK
        assert order.price == 0.34  # Uses market yes_price
        assert order.size == 10
        assert order.fee_rate_bps == 700

    def test_buy_no_market_order(self, builder, sample_market, signal_buy_no):
        order = builder.build_market_order(sample_market, signal_buy_no, size=5)

        assert order.token_id == "FED-RATE-CUT-MAY26_no"
        assert order.price == 0.66  # Uses market no_price

    def test_paper_mode_flag(self, builder, sample_market, signal_buy_yes):
        order = builder.build_market_order(sample_market, signal_buy_yes, 1)
        assert order.paper is True


class TestResolveSideAndToken:
    def test_all_directions(self, builder, sample_market):
        buy_yes = builder._resolve_side_and_token(sample_market, Direction.BUY_YES)
        assert buy_yes == (Side.BUY, "FED-RATE-CUT-MAY26_yes")

        buy_no = builder._resolve_side_and_token(sample_market, Direction.BUY_NO)
        assert buy_no == (Side.BUY, "FED-RATE-CUT-MAY26_no")

        sell_yes = builder._resolve_side_and_token(sample_market, Direction.SELL_YES)
        assert sell_yes == (Side.SELL, "FED-RATE-CUT-MAY26_yes")

        sell_no = builder._resolve_side_and_token(sample_market, Direction.SELL_NO)
        assert sell_no == (Side.SELL, "FED-RATE-CUT-MAY26_no")
