"""Tests for risk engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.core.models import (
    Direction,
    Market,
    MarketCategory,
    MarketToken,
    Signal,
    StrategyName,
)
from src.execution.position_manager import PositionManager
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.risk_engine import RiskEngine


@pytest.fixture
def position_manager(tmp_db) -> PositionManager:
    return PositionManager(tmp_db, bankroll=500.0)


@pytest.fixture
def circuit_breaker(settings, tmp_db) -> CircuitBreaker:
    return CircuitBreaker(settings, tmp_db)


@pytest.fixture
def engine(settings, position_manager, circuit_breaker, tmp_db) -> RiskEngine:
    return RiskEngine(settings, position_manager, circuit_breaker, tmp_db)


@pytest.fixture
def signal() -> Signal:
    return Signal(
        strategy=StrategyName.AI_PROBABILITY,
        market_id="FED-RATE-CUT-MAY26",
        direction=Direction.BUY_YES,
        edge=0.08,
        probability_estimate=0.42,
        market_price=0.34,
        confidence=0.7,
    )


@pytest.fixture
def market() -> Market:
    return Market(
        ticker="FED-RATE-CUT-MAY26",
        question="Will the Fed cut rates?",
        tokens=[
            MarketToken(token_id="FED-RATE-CUT-MAY26_yes", outcome="Yes", price=0.34),
            MarketToken(token_id="FED-RATE-CUT-MAY26_no", outcome="No", price=0.66),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=45),
        volume_24h=100000,
        liquidity=50000,
        active=True,
    )


class TestCheckAll:
    def test_passes_valid_trade(self, engine, signal, market):
        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is True
        assert result.approved_size == 10
        assert len(result.failed_checks) == 0

    def test_fails_insufficient_balance(self, engine, signal, market):
        result = engine.check_all(signal, market, proposed_size=2000, proposed_cost=600.0)
        assert result.passed is False
        assert any("Insufficient balance" in c for c in result.failed_checks)

    def test_fails_position_too_large(self, engine, signal, market):
        # Max position = 500 * 0.05 = $25
        result = engine.check_all(signal, market, proposed_size=100, proposed_cost=30.0)
        assert result.passed is False
        assert any("Position too large" in c for c in result.failed_checks)

    def test_fails_total_exposure(self, engine, signal, market, position_manager, tmp_db):
        from src.core.models import Side, Trade

        # Fill up exposure to near limit
        for i in range(8):
            trade = Trade(
                order_id=f"PE-{i}", market_id=f"MKT-{i}", token_id=f"MKT-{i}_yes",
                side=Side.BUY, price=0.50, size=50, strategy=StrategyName.AI_PROBABILITY, paper=True,
            )
            position_manager.update_from_trade(trade)

        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("Total exposure" in c for c in result.failed_checks)

    def test_fails_circuit_breaker(self, engine, signal, market, circuit_breaker):
        circuit_breaker._halted = True
        circuit_breaker._halt_reason = "Daily loss limit"

        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("Circuit breaker" in c for c in result.failed_checks)

    def test_existing_position_warns_but_passes_by_default(
        self, engine, signal, market, position_manager
    ):
        """With allow_position_additions=True (default), same-direction addition warns but passes."""
        from src.core.models import Side, Trade
        trade = Trade(
            order_id="PE-x", market_id="FED-RATE-CUT-MAY26",
            token_id="FED-RATE-CUT-MAY26_yes", side=Side.BUY,
            price=0.34, size=5, strategy=StrategyName.AI_PROBABILITY, paper=True,
        )
        position_manager.update_from_trade(trade)

        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is True
        assert not any("Already have position" in c for c in result.failed_checks)
        assert any("Adding to existing" in w for w in result.warnings)

    def test_existing_position_hedge_warns_but_passes_by_default(
        self, engine, market, position_manager, settings
    ):
        """With allow_position_additions=True (default), a hedge warns but passes."""
        from src.core.models import Direction, Side, Trade

        # Establish a BUY_YES position
        trade = Trade(
            order_id="PE-x", market_id="FED-RATE-CUT-MAY26",
            token_id="FED-RATE-CUT-MAY26_yes", side=Side.BUY,
            price=0.34, size=5, strategy=StrategyName.AI_PROBABILITY, paper=True,
        )
        position_manager.update_from_trade(trade)

        # Now signal a BUY_NO (hedge)
        hedge_signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="FED-RATE-CUT-MAY26",
            direction=Direction.BUY_NO,
            edge=0.08,
            probability_estimate=0.58,
            market_price=0.66,
            confidence=0.7,
        )
        result = engine.check_all(hedge_signal, market, proposed_size=10, proposed_cost=6.60)
        assert result.passed is True
        assert not any("Already have position" in c for c in result.failed_checks)
        assert any("Hedge detected" in w for w in result.warnings)

    def test_existing_position_fails_when_additions_disabled(
        self, signal, market, position_manager, circuit_breaker, tmp_db
    ):
        """With allow_position_additions=False, any addition to an existing position is rejected."""
        from src.config import Settings, TradingConfig
        from src.core.models import Side, Trade

        strict_settings = Settings(trading=TradingConfig(allow_position_additions=False))
        strict_engine = RiskEngine(strict_settings, position_manager, circuit_breaker, tmp_db)

        trade = Trade(
            order_id="PE-x", market_id="FED-RATE-CUT-MAY26",
            token_id="FED-RATE-CUT-MAY26_yes", side=Side.BUY,
            price=0.34, size=5, strategy=StrategyName.AI_PROBABILITY, paper=True,
        )
        position_manager.update_from_trade(trade)

        result = strict_engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("Already have position" in c for c in result.failed_checks)

    def test_fails_edge_too_small(self, engine, market):
        weak_signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="FED-RATE-CUT-MAY26",
            direction=Direction.BUY_YES,
            edge=0.02,  # Below 5% minimum
            probability_estimate=0.36,
            market_price=0.34,
        )
        result = engine.check_all(weak_signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("Edge too small" in c for c in result.failed_checks)

    def test_fails_market_resolving_within_4_hours(self, engine, signal):
        """M-5: Markets resolving in <4 hours are rejected outright."""
        expiring_market = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Resolving very soon",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(hours=2),
            liquidity=50000,
        )
        result = engine.check_all(signal, expiring_market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("<4 hours" in c for c in result.failed_checks)

    def test_warns_market_resolving_under_1_day(self, engine, signal):
        """M-5: Markets resolving in <1 day (but >4 hours) get a warning but pass."""
        soon_market = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Resolving soon",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(hours=12),
            liquidity=50000,
        )
        result = engine.check_all(signal, soon_market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is True
        assert any("<1 day" in w for w in result.warnings)

    def test_fails_cooldown(self, engine, signal, market):
        engine.record_exit("FED-RATE-CUT-MAY26")

        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("Cooldown" in c for c in result.failed_checks)

    def test_fails_liquidity(self, engine, signal):
        thin_market = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Thin market",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=45),
            liquidity=20.0,
        )
        result = engine.check_all(signal, thin_market, proposed_size=100, proposed_cost=34.0)
        assert result.passed is False
        assert any("liquidity" in c.lower() for c in result.failed_checks)

    def test_obvious_no_exposure_limit(self, engine, market, position_manager):
        from src.core.models import Side, Trade

        # Fill up obvious NO exposure
        trade = Trade(
            order_id="PE-no1", market_id="OTHER-MKT",
            token_id="OTHER-MKT_no", side=Side.BUY,
            price=0.97, size=50, strategy=StrategyName.OBVIOUS_NO, paper=True,
        )
        position_manager.update_from_trade(trade)

        no_signal = Signal(
            strategy=StrategyName.OBVIOUS_NO,
            market_id="FED-RATE-CUT-MAY26",
            direction=Direction.BUY_NO,
            edge=0.03,
            probability_estimate=0.97,
            market_price=0.66,
        )
        result = engine.check_all(no_signal, market, proposed_size=10, proposed_cost=9.70)
        assert result.passed is False
        assert any("Obvious NO" in c for c in result.failed_checks)

    def test_fails_correlated_exposure(self, engine, signal, market, position_manager):
        """Filling AI_PROBABILITY to near 20% limit should block another AI_PROBABILITY trade.

        With 50% correlation fallback, need raw exposure > 2x the limit so that
        effective_correlated (50%) + proposed_cost > max_correlated ($100).
        """
        from src.core.models import Side, Trade

        # Fill up strategy exposure: 8 x $25 = $200 raw → $100 effective (50%)
        for i in range(8):
            trade = Trade(
                order_id=f"PE-corr-{i}", market_id=f"AI-MKT-{i}",
                token_id=f"AI-MKT-{i}_yes", side=Side.BUY,
                price=0.50, size=50,  # $25 each = $200 total raw
                strategy=StrategyName.AI_PROBABILITY, paper=True,
            )
            position_manager.update_from_trade(trade)

        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("Correlated exposure" in c for c in result.failed_checks)

    def test_warns_long_dated(self, engine, signal):
        long_market = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Long dated",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=400),
            liquidity=50000,
        )
        result = engine.check_all(signal, long_market, proposed_size=10, proposed_cost=3.40)
        assert any("Long-dated" in w for w in result.warnings)


class TestNegativeEdgeRejection:
    """Regression: negative edge signals must be rejected (not just small ones)."""

    def test_negative_edge_rejected(self, engine, market):
        neg_signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="FED-RATE-CUT-MAY26",
            direction=Direction.BUY_YES,
            edge=-0.03,
            probability_estimate=0.31,
            market_price=0.34,
        )
        result = engine.check_all(neg_signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("non-positive edge" in c for c in result.failed_checks)

    def test_zero_edge_rejected(self, engine, market):
        zero_signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="FED-RATE-CUT-MAY26",
            direction=Direction.BUY_YES,
            edge=0.0,
            probability_estimate=0.34,
            market_price=0.34,
        )
        result = engine.check_all(zero_signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False


class TestNaNEdgeRejection:
    """Regression: NaN/inf edge must be rejected at model creation time.
    Signal validator now rejects non-finite edges before they reach risk engine."""

    def test_nan_edge_rejected(self, engine, market):
        import pytest
        with pytest.raises(Exception):  # ValidationError from Pydantic
            Signal(
                strategy=StrategyName.AI_PROBABILITY,
                market_id="FED-RATE-CUT-MAY26",
                direction=Direction.BUY_YES,
                edge=float("nan"),
                probability_estimate=0.42,
                market_price=0.34,
            )

    def test_inf_edge_rejected(self, engine, market):
        import pytest
        with pytest.raises(Exception):  # ValidationError from Pydantic
            Signal(
                strategy=StrategyName.AI_PROBABILITY,
                market_id="FED-RATE-CUT-MAY26",
                direction=Direction.BUY_YES,
                edge=float("inf"),
                probability_estimate=0.42,
                market_price=0.34,
            )


class TestCooldownPersistence:
    def test_cooldown_survives_restart(self, settings, tmp_db):
        """Cooldowns should persist across RiskEngine restarts."""
        pm = PositionManager(tmp_db, bankroll=500.0)
        cb = CircuitBreaker(settings, tmp_db)

        engine1 = RiskEngine(settings, pm, cb, tmp_db)
        engine1.record_exit("FED-RATE-CUT-MAY26")

        # Simulate restart: new instance on same DB
        engine2 = RiskEngine(settings, pm, cb, tmp_db)
        assert "FED-RATE-CUT-MAY26" in engine2._cooldowns

        # Verify it actually blocks
        sig = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="FED-RATE-CUT-MAY26",
            direction=Direction.BUY_YES,
            edge=0.08,
            probability_estimate=0.42,
            market_price=0.34,
        )
        mkt = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Test",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=45),
            liquidity=50000,
        )
        result = engine2.check_all(sig, mkt, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("Cooldown" in c for c in result.failed_checks)


class TestEdgeProbabilityValidation:
    def test_edge_exceeding_probability_rejected(self, engine, market):
        """Edge cannot exceed probability_estimate — implies negative market price."""
        bad_signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id=market.ticker,
            direction=Direction.BUY_YES,
            edge=0.60,
            probability_estimate=0.50,
            market_price=0.34,
        )
        result = engine.check_all(bad_signal, market, proposed_size=5, proposed_cost=1.70)
        assert result.passed is False
        assert any("implies market_price" in c for c in result.failed_checks)

    def test_edge_equal_probability_rejected(self, engine, market):
        """Edge == probability_estimate implies market_price == 0 — should be rejected."""
        bad_signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id=market.ticker,
            direction=Direction.BUY_YES,
            edge=0.50,
            probability_estimate=0.50,
            market_price=0.34,
        )
        result = engine.check_all(bad_signal, market, proposed_size=5, proposed_cost=1.70)
        assert result.passed is False
        assert any("implies market_price" in c for c in result.failed_checks)

    def test_edge_within_probability_passes_edge_check(self, engine, market):
        """Edge < probability should not trigger the edge-exceeds-probability check."""
        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id=market.ticker,
            direction=Direction.BUY_YES,
            edge=0.08,
            probability_estimate=0.42,
            market_price=0.34,
        )
        result = engine.check_all(signal, market, proposed_size=5, proposed_cost=1.70)
        assert not any("implies market_price" in c for c in result.failed_checks)


class TestExcludedCategory:
    """H-1: Defense-in-depth — risk engine rejects markets in excluded categories."""

    def test_rejects_crypto_category(self, engine, signal):
        crypto_market = Market(
            ticker="BTC-PRICE-100K",
            question="Will BTC hit $100K?",
            category=MarketCategory.CRYPTO,
            tokens=[
                MarketToken(token_id="BTC-yes", outcome="Yes", price=0.50),
                MarketToken(token_id="BTC-no", outcome="No", price=0.50),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
            volume_24h=100000,
            liquidity=50000,
        )
        result = engine.check_all(signal, crypto_market, proposed_size=5, proposed_cost=2.50)
        assert result.passed is False
        assert any("Excluded category" in c for c in result.failed_checks)

    def test_rejects_sports_category(self, engine, signal):
        sports_market = Market(
            ticker="NBA-FINALS",
            question="Will the Lakers win?",
            category=MarketCategory.SPORTS,
            tokens=[
                MarketToken(token_id="NBA-yes", outcome="Yes", price=0.40),
                MarketToken(token_id="NBA-no", outcome="No", price=0.60),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=14),
            volume_24h=100000,
            liquidity=50000,
        )
        result = engine.check_all(signal, sports_market, proposed_size=5, proposed_cost=2.00)
        assert result.passed is False
        assert any("Excluded category" in c for c in result.failed_checks)

    def test_rejects_excluded_tag(self, engine, signal):
        tagged_market = Market(
            ticker="SOME-MARKET",
            question="Some crypto question",
            category=MarketCategory.OTHER,
            tags=["Crypto Prices", "Bitcoin"],
            tokens=[
                MarketToken(token_id="SOME-yes", outcome="Yes", price=0.50),
                MarketToken(token_id="SOME-no", outcome="No", price=0.50),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
            volume_24h=100000,
            liquidity=50000,
        )
        result = engine.check_all(signal, tagged_market, proposed_size=5, proposed_cost=2.50)
        assert result.passed is False
        assert any("Excluded category" in c for c in result.failed_checks)

    def test_allows_politics_category(self, engine, signal, market):
        """Politics is an allowed category — should not be blocked."""
        market.category = MarketCategory.POLITICS
        result = engine.check_all(signal, market, proposed_size=5, proposed_cost=1.70)
        assert not any("Excluded category" in c for c in result.failed_checks)

    def test_allows_fed_macro_category(self, engine, signal, market):
        market.category = MarketCategory.FED_MACRO
        result = engine.check_all(signal, market, proposed_size=5, proposed_cost=1.70)
        assert not any("Excluded category" in c for c in result.failed_checks)

    def test_case_insensitive_match(self, engine, signal):
        """Exclusion matching should be case-insensitive."""
        market = Market(
            ticker="CRYPTO-TEST",
            question="Test",
            category=MarketCategory.CRYPTO,
            tokens=[
                MarketToken(token_id="t-yes", outcome="Yes", price=0.50),
                MarketToken(token_id="t-no", outcome="No", price=0.50),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
            volume_24h=100000,
            liquidity=50000,
        )
        result = engine.check_all(signal, market, proposed_size=5, proposed_cost=2.50)
        assert result.passed is False


class TestResolutionDateBoundaries:
    """M-5: Markets resolving within 4 hours are rejected, <1 day warns."""

    def test_rejects_market_resolving_in_2_hours(self, engine, signal):
        market = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Imminently resolving",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(hours=2),
            liquidity=50000,
        )
        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("<4 hours" in c for c in result.failed_checks)

    def test_rejects_market_resolving_in_3_hours(self, engine, signal):
        market = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Resolving in 3 hours",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(hours=3),
            liquidity=50000,
        )
        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is False
        assert any("<4 hours" in c for c in result.failed_checks)

    def test_warns_market_resolving_in_6_hours(self, engine, signal):
        """6 hours = 0.25 days: above 4h threshold but below 1 day -> warning, not failure."""
        market = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Resolving in 6 hours",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(hours=6),
            liquidity=50000,
        )
        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert result.passed is True
        assert any("<1 day" in w for w in result.warnings)

    def test_boundary_at_4_hours(self, engine, signal):
        """Exactly at the 4-hour boundary (0.167 days) should pass (>= 0.167)."""
        market = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Resolving at boundary",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(hours=4, minutes=5),
            liquidity=50000,
        )
        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        # Should not fail on resolution date (but may warn <1 day)
        assert not any("<4 hours" in c for c in result.failed_checks)

    def test_passes_market_resolving_in_2_days(self, engine, signal):
        market = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Normal resolution",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.34),
                MarketToken(token_id="no", outcome="No", price=0.66),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=2),
            liquidity=50000,
        )
        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=3.40)
        assert not any("<4 hours" in c for c in result.failed_checks)
        assert not any("<1 day" in w for w in result.warnings)


class TestSpreadVsEdge:
    """Reject trades where bid-ask spread eats >50% of edge."""

    def test_wide_spread_rejected(self, engine, signal):
        """Spread=4% with edge=5% → 80% consumed → reject."""
        signal.edge = 0.05
        market = Market(
            ticker="WIDE-SPREAD",
            question="Wide spread market",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.50),
                MarketToken(token_id="no", outcome="No", price=0.46),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=7),
            liquidity=50000,
            spread=0.04,
        )
        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=5.00)
        assert result.passed is False
        assert any("Spread too wide" in c for c in result.failed_checks)

    def test_tight_spread_passes(self, engine, signal):
        """Spread=1% with edge=5% → 20% consumed → OK."""
        signal.edge = 0.05
        market = Market(
            ticker="TIGHT-SPREAD",
            question="Tight spread market",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.50),
                MarketToken(token_id="no", outcome="No", price=0.49),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=7),
            liquidity=50000,
        )
        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=5.00)
        assert not any("Spread too wide" in c for c in result.failed_checks)

    def test_zero_spread_passes(self, engine, signal):
        """Zero spread should pass."""
        signal.edge = 0.05
        market = Market(
            ticker="ZERO-SPREAD",
            question="Zero spread market",
            tokens=[
                MarketToken(token_id="yes", outcome="Yes", price=0.50),
                MarketToken(token_id="no", outcome="No", price=0.50),
            ],
            end_date=datetime.now(timezone.utc) + timedelta(days=7),
            liquidity=50000,
        )
        result = engine.check_all(signal, market, proposed_size=10, proposed_cost=5.00)
        assert not any("Spread too wide" in c for c in result.failed_checks)
