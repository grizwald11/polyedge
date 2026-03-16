"""Tests for core data models."""

from datetime import datetime, timedelta

from src.core.models import (
    Market, MarketToken, MarketCategory, Signal, StrategyName, Direction,
    Order, Side, OrderType, OrderStatus, CalibrationRecord, ForecastResult,
    RiskCheckResult, Position, MarketSnapshot,
)


class TestMarketModel:
    def test_basic_construction(self, sample_market):
        assert sample_market.condition_id == "0xabc123def456"
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
        m = Market(condition_id="x", question="test")
        assert m.days_to_resolution is None

    def test_non_binary_market(self):
        m = Market(
            condition_id="x",
            question="Who wins?",
            tokens=[
                MarketToken(token_id="a", outcome="Alice", price=0.4),
                MarketToken(token_id="b", outcome="Bob", price=0.3),
                MarketToken(token_id="c", outcome="Carol", price=0.3),
            ],
        )
        assert m.is_binary is False

    def test_empty_tokens(self):
        m = Market(condition_id="x", question="test")
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
        assert abs(p.cost_basis - 40.0) < 0.01
