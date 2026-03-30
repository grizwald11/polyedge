"""Tests for core data models."""

from datetime import datetime, timedelta

import pytest

from src.core.models import (
    Market, MarketToken, MarketCategory, Signal, StrategyName, Direction,
    Order, Side, OrderType, OrderStatus, CalibrationRecord, ForecastResult,
    RiskCheckResult, Position, MarketSnapshot,
    cents_to_dollars, dollars_to_cents, kalshi_taker_fee, kalshi_maker_fee,
)


class TestPriceConversion:
    def test_cents_to_dollars(self):
        assert cents_to_dollars(50) == 0.50
        assert cents_to_dollars(1) == 0.01
        assert cents_to_dollars(99) == 0.99

    def test_dollars_to_cents(self):
        assert dollars_to_cents(0.50) == 50
        assert dollars_to_cents(0.01) == 1
        assert dollars_to_cents(0.99) == 99

    def test_roundtrip(self):
        for c in [1, 25, 50, 75, 99]:
            assert dollars_to_cents(cents_to_dollars(c)) == c


class TestFeeCalculation:
    def test_taker_fee_midprice(self):
        # At 50 cents, 1 contract: ceil(0.07 * 1 * 0.50 * 0.50) = ceil(0.0175) = 1
        fee = kalshi_taker_fee(1, 50)
        assert fee == 1

    def test_taker_fee_extreme_price(self):
        # At 90 cents, 1 contract: ceil(0.07 * 1 * 0.90 * 0.10) = ceil(0.0063) = 1
        fee = kalshi_taker_fee(1, 90)
        assert fee == 1

    def test_taker_fee_multiple_contracts(self):
        # At 50 cents, 10 contracts: ceil(0.07 * 10 * 0.50 * 0.50) = ceil(0.175) = 1
        fee = kalshi_taker_fee(10, 50)
        assert fee == 1

    def test_maker_fee(self):
        # At 50 cents, 10 contracts: ceil(0.0175 * 10 * 0.50 * 0.50) = ceil(0.04375) = 1
        fee = kalshi_maker_fee(10, 50)
        assert fee == 1


class TestMarketModel:
    def test_basic_construction(self, sample_market):
        assert sample_market.ticker == "FED-RATE-CUT-MAY26"
        assert sample_market.category == MarketCategory.FED_MACRO
        assert sample_market.active is True
        assert len(sample_market.tokens) == 2

    def test_yes_no_tokens(self, sample_market):
        assert sample_market.yes_token is not None
        assert sample_market.yes_token.outcome == "Yes"
        assert sample_market.yes_token.price == 0.34
        assert sample_market.no_token is not None
        assert sample_market.no_token.price == 0.66

    def test_convenience_properties(self, sample_market):
        assert sample_market.yes_price == 0.34
        assert sample_market.no_price == 0.66
        assert sample_market.implied_probability == 0.34
        assert sample_market.is_binary is True

    def test_days_to_resolution(self, sample_market):
        days = sample_market.days_to_resolution
        assert days is not None
        assert 44 <= days <= 46  # ~45 days

    def test_days_to_resolution_none(self):
        m = Market(ticker="x", question="test")
        assert m.days_to_resolution is None

    def test_non_binary_market(self):
        m = Market(
            ticker="x",
            question="Who wins?",
            tokens=[
                MarketToken(token_id="a", outcome="Yes", price=0.4),
                MarketToken(token_id="b", outcome="No", price=0.3),
                MarketToken(token_id="c", outcome="Yes", price=0.3),
            ],
        )
        assert m.is_binary is False

    def test_empty_tokens(self):
        m = Market(ticker="x", question="test")
        assert m.yes_token is None
        assert m.no_token is None
        assert m.yes_price == 0.0


class TestSignalModel:
    def test_construction(self, sample_signal):
        assert sample_signal.strategy == StrategyName.AI_PROBABILITY
        assert sample_signal.direction == Direction.BUY_YES
        assert sample_signal.edge == 0.08
        assert sample_signal.acted_on is False

    def test_timestamp_auto(self):
        s = Signal(
            strategy=StrategyName.OBVIOUS_NO,
            market_id="x",
            direction=Direction.BUY_NO,
            edge=0.03,
            probability_estimate=0.97,
            market_price=0.97,
        )
        assert s.timestamp is not None
        assert isinstance(s.timestamp, datetime)


class TestCalibrationRecord:
    def test_brier_score_correct_yes(self):
        r = CalibrationRecord(
            market_id="x",
            predicted_probability=0.7,
            market_price_at_prediction=0.5,
            actual_outcome=True,
        )
        # (0.7 - 1.0)^2 = 0.09
        assert abs(r.brier_score - 0.09) < 0.001

    def test_brier_score_correct_no(self):
        r = CalibrationRecord(
            market_id="x",
            predicted_probability=0.3,
            market_price_at_prediction=0.5,
            actual_outcome=False,
        )
        # (0.3 - 0.0)^2 = 0.09
        assert abs(r.brier_score - 0.09) < 0.001

    def test_brier_score_wrong(self):
        r = CalibrationRecord(
            market_id="x",
            predicted_probability=0.9,
            market_price_at_prediction=0.5,
            actual_outcome=False,
        )
        # (0.9 - 0.0)^2 = 0.81
        assert abs(r.brier_score - 0.81) < 0.001

    def test_brier_score_unresolved(self):
        r = CalibrationRecord(
            market_id="x",
            predicted_probability=0.5,
            market_price_at_prediction=0.5,
        )
        assert r.brier_score is None
        assert r.is_resolved is False


class TestForecastResult:
    def test_construction(self, sample_forecast):
        assert sample_forecast.probability == 0.42
        assert len(sample_forecast.key_factors_for) == 2
        assert sample_forecast.model_used == "claude-sonnet-4-6"

    def test_probability_out_of_range(self):
        with pytest.raises(ValueError, match="probability"):
            ForecastResult(probability=1.5, reasoning="test")

    def test_negative_probability_rejected(self):
        with pytest.raises(ValueError, match="probability"):
            ForecastResult(probability=-0.1, reasoning="test")

    def test_inverted_ci_bounds_auto_corrected(self):
        """Regression: inverted CI (low=0.8, high=0.2) previously corrupted ensemble weights.

        ci_width became negative → ci_penalty=0 → Claude got full weight
        regardless of uncertainty. Now auto-corrected to low=0.8, high=0.8.
        """
        f = ForecastResult(
            probability=0.50,
            confidence_low=0.80,
            confidence_high=0.20,
            reasoning="test",
        )
        # Auto-corrected: high should be clamped to at least confidence_low
        assert f.confidence_high >= f.confidence_low

    def test_ci_bounds_clamped_to_01(self):
        f = ForecastResult(
            probability=0.50,
            confidence_low=-0.5,
            confidence_high=1.5,
            reasoning="test",
        )
        assert f.confidence_low == 0.0
        assert f.confidence_high == 1.0

    def test_valid_ci_preserved(self):
        f = ForecastResult(
            probability=0.60,
            confidence_low=0.50,
            confidence_high=0.70,
            reasoning="test",
        )
        assert f.confidence_low == 0.50
        assert f.confidence_high == 0.70


class TestRiskCheckResult:
    def test_passed(self):
        r = RiskCheckResult(passed=True, approved_size=25.0)
        assert r.passed is True
        assert r.approved_size == 25.0
        assert len(r.failed_checks) == 0

    def test_failed(self):
        r = RiskCheckResult(
            passed=False,
            failed_checks=["max_position_exceeded", "daily_loss_limit"],
            warnings=["near_exposure_limit"],
        )
        assert r.passed is False
        assert len(r.failed_checks) == 2
        assert len(r.warnings) == 1


class TestPositionModel:
    def test_market_value(self):
        p = Position(
            market_id="x", token_id="t", direction=Direction.BUY_YES,
            size=100, avg_entry_price=0.40, current_price=0.55,
        )
        assert abs(p.market_value - 55.0) < 0.01
        assert abs(p.cost_basis - 40.0) < 0.01  # No fees → same as before

    def test_cost_basis_includes_fees(self):
        p = Position(
            market_id="x", token_id="t", direction=Direction.BUY_YES,
            size=100, avg_entry_price=0.40, total_fees=1.50, buy_fees=1.50,
        )
        # cost_basis = size * avg_entry + total_fees = 40.0 + 1.50
        assert abs(p.cost_basis - 41.50) < 0.01

    def test_total_fees_default_zero(self):
        p = Position(
            market_id="x", token_id="t", direction=Direction.BUY_YES,
            size=10, avg_entry_price=0.50,
        )
        assert p.total_fees == 0.0


class TestSignalValidation:
    def test_probability_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="probability_estimate"):
            Signal(
                strategy=StrategyName.AI_PROBABILITY,
                market_id="X", direction=Direction.BUY_YES,
                edge=0.1, probability_estimate=1.5, market_price=0.5,
            )

    def test_market_price_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="market_price"):
            Signal(
                strategy=StrategyName.AI_PROBABILITY,
                market_id="X", direction=Direction.BUY_YES,
                edge=0.1, probability_estimate=0.5, market_price=-0.1,
            )

    def test_confidence_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="confidence"):
            Signal(
                strategy=StrategyName.AI_PROBABILITY,
                market_id="X", direction=Direction.BUY_YES,
                edge=0.1, probability_estimate=0.5, market_price=0.5,
                confidence=2.0,
            )

    def test_valid_signal_accepted(self):
        s = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="X", direction=Direction.BUY_YES,
            edge=0.1, probability_estimate=0.6, market_price=0.5,
            confidence=0.8,
        )
        assert s.probability_estimate == 0.6


class TestOrderValidation:
    def test_negative_price_rejected(self):
        with pytest.raises(ValueError, match="price"):
            Order(
                market_id="X", token_id="t", side=Side.BUY,
                price=-0.5, size=10,
            )

    def test_price_above_99_rejected(self):
        """Kalshi prices are 0.01-0.99. Prices > 0.99 should be rejected."""
        with pytest.raises(ValueError, match="price"):
            Order(
                market_id="X", token_id="t", side=Side.BUY,
                price=1.50, size=10,
            )

    def test_negative_size_rejected(self):
        with pytest.raises(ValueError, match="size"):
            Order(
                market_id="X", token_id="t", side=Side.BUY,
                price=0.5, size=-1,
            )

    def test_valid_order_accepted(self):
        o = Order(market_id="X", token_id="t", side=Side.BUY, price=0.5, size=10)
        assert o.price == 0.5
        assert o.size == 10
