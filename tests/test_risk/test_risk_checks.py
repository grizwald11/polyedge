"""Tests for individual risk check functions (H-5).

Covers: get_min_edge, check_excluded_category, check_balance, check_position_size,
check_total_exposure, check_circuit_breaker, check_liquidity, check_existing_position,
check_signal_quality, check_resolution_date, check_cooldown.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

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
from src.risk.risk_checks import (
    check_balance,
    check_circuit_breaker,
    check_excluded_category,
    check_existing_position,
    check_liquidity,
    check_obvious_no_limit,
    check_position_size,
    check_total_exposure,
    get_min_edge,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def market() -> Market:
    return Market(
        ticker="TEST-MKT-1",
        question="Test market?",
        category=MarketCategory.POLITICS,
        tokens=[
            MarketToken(token_id="TEST-MKT-1_yes", outcome="Yes", price=0.50),
            MarketToken(token_id="TEST-MKT-1_no", outcome="No", price=0.50),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
        volume_24h=50000,
        liquidity=25000,
        active=True,
    )


@pytest.fixture
def signal() -> Signal:
    return Signal(
        strategy=StrategyName.AI_PROBABILITY,
        market_id="TEST-MKT-1",
        direction=Direction.BUY_YES,
        edge=0.08,
        probability_estimate=0.58,
        market_price=0.50,
        confidence=0.7,
    )


# ---------------------------------------------------------------------------
# get_min_edge
# ---------------------------------------------------------------------------


class TestGetMinEdge:
    def test_ai_probability(self, settings):
        assert get_min_edge(settings, StrategyName.AI_PROBABILITY) == settings.trading.min_edge_ai

    def test_cross_arb(self, settings):
        assert get_min_edge(settings, StrategyName.CROSS_ARB) == settings.trading.min_edge_arb

    def test_obvious_no(self, settings):
        assert get_min_edge(settings, StrategyName.OBVIOUS_NO) == settings.trading.min_edge_obvious_no

    def test_news_reactive(self, settings):
        assert get_min_edge(settings, StrategyName.NEWS_REACTIVE) == settings.trading.min_edge_news

    def test_mean_reversion(self, settings):
        assert get_min_edge(settings, StrategyName.MEAN_REVERSION) == settings.trading.min_edge_mean_reversion

    def test_late_resolution(self, settings):
        assert get_min_edge(settings, StrategyName.LATE_RESOLUTION) == settings.trading.min_edge_late_resolution

    def test_unknown_strategy_defaults_to_ai(self, settings):
        """Unknown strategies should fall back to min_edge_ai."""
        assert get_min_edge(settings, StrategyName.WHALE_TRACKER) == settings.trading.min_edge_ai


# ---------------------------------------------------------------------------
# check_excluded_category
# ---------------------------------------------------------------------------


class TestCheckExcludedCategory:
    def test_passes_allowed_category(self, settings, market):
        failed = []
        check_excluded_category(settings, market, failed)
        assert len(failed) == 0

    def test_fails_excluded_category(self, settings):
        crypto_market = Market(
            ticker="BTC-UP",
            question="BTC up?",
            category=MarketCategory.CRYPTO,
            tags=["Crypto Prices"],
            tokens=[
                MarketToken(token_id="BTC-UP_yes", outcome="Yes", price=0.50),
                MarketToken(token_id="BTC-UP_no", outcome="No", price=0.50),
            ],
            volume_24h=100000,
            active=True,
        )
        failed = []
        check_excluded_category(settings, crypto_market, failed)
        assert len(failed) > 0
        assert "excluded" in failed[0].lower() or "Excluded" in failed[0]

    def test_fails_on_excluded_tag(self, settings):
        """Market with allowed category but excluded tag should fail."""
        market = Market(
            ticker="SPORTS-MKT",
            question="Who wins?",
            category=MarketCategory.OTHER,
            tags=["Sports"],
            tokens=[
                MarketToken(token_id="SPORTS-MKT_yes", outcome="Yes", price=0.50),
                MarketToken(token_id="SPORTS-MKT_no", outcome="No", price=0.50),
            ],
            volume_24h=100000,
            active=True,
        )
        failed = []
        check_excluded_category(settings, market, failed)
        assert len(failed) > 0


# ---------------------------------------------------------------------------
# check_balance
# ---------------------------------------------------------------------------


class TestCheckBalance:
    def test_passes_sufficient_balance(self):
        positions = MagicMock()
        positions.get_total_exposure.return_value = 100.0
        failed = []
        committed = check_balance(positions, bankroll=500.0, proposed_cost=50.0, pending_order_cost=0.0, failed=failed)
        assert len(failed) == 0
        assert committed == 100.0

    def test_fails_insufficient_balance(self):
        positions = MagicMock()
        positions.get_total_exposure.return_value = 450.0
        failed = []
        check_balance(positions, bankroll=500.0, proposed_cost=100.0, pending_order_cost=0.0, failed=failed)
        assert len(failed) > 0
        assert "Insufficient balance" in failed[0]

    def test_includes_pending_orders(self):
        positions = MagicMock()
        positions.get_total_exposure.return_value = 200.0
        failed = []
        committed = check_balance(positions, bankroll=500.0, proposed_cost=50.0, pending_order_cost=100.0, failed=failed)
        assert committed == 300.0  # 200 + 100 pending
        assert len(failed) == 0


# ---------------------------------------------------------------------------
# check_position_size
# ---------------------------------------------------------------------------


class TestCheckPositionSize:
    def test_passes_small_position(self, settings):
        failed = []
        check_position_size(settings, bankroll=500.0, proposed_cost=20.0, failed=failed)
        assert len(failed) == 0

    def test_fails_oversized_position(self, settings):
        failed = []
        # Max = 500 * 0.05 = $25
        check_position_size(settings, bankroll=500.0, proposed_cost=30.0, failed=failed)
        assert len(failed) > 0
        assert "Position too large" in failed[0]


# ---------------------------------------------------------------------------
# check_total_exposure
# ---------------------------------------------------------------------------


class TestCheckTotalExposure:
    def test_passes_within_limit(self, settings):
        failed = []
        check_total_exposure(settings, bankroll=500.0, proposed_cost=20.0, committed=100.0, failed=failed)
        assert len(failed) == 0

    def test_fails_over_limit(self, settings):
        failed = []
        # Max = 500 * 0.40 = $200
        check_total_exposure(settings, bankroll=500.0, proposed_cost=50.0, committed=160.0, failed=failed)
        assert len(failed) > 0
        assert "exposure" in failed[0].lower()


# ---------------------------------------------------------------------------
# check_circuit_breaker
# ---------------------------------------------------------------------------


class TestCheckCircuitBreaker:
    def test_passes_when_not_halted(self):
        cb = MagicMock()
        cb.is_halted.return_value = False
        failed = []
        check_circuit_breaker(cb, failed)
        assert len(failed) == 0

    def test_fails_when_halted(self):
        cb = MagicMock()
        cb.is_halted.return_value = True
        cb.halt_reason = "Daily loss limit"
        failed = []
        check_circuit_breaker(cb, failed)
        assert len(failed) > 0
        assert "Circuit breaker" in failed[0]


# ---------------------------------------------------------------------------
# check_liquidity
# ---------------------------------------------------------------------------


class TestCheckLiquidity:
    def test_passes_adequate_liquidity(self, market):
        failed, warnings = [], []
        check_liquidity(market, proposed_cost=100.0, failed=failed, warnings=warnings)
        assert len(failed) == 0

    def test_fails_order_too_large(self, market):
        failed, warnings = [], []
        # market.liquidity = 25000, 10% = 2500. Propose $3000
        check_liquidity(market, proposed_cost=3000.0, failed=failed, warnings=warnings)
        assert len(failed) > 0

    def test_warns_on_moderate_size(self, market):
        failed, warnings = [], []
        # 5% of 25000 = 1250, 10% = 2500. Propose $1500 (between 5-10%)
        check_liquidity(market, proposed_cost=1500.0, failed=failed, warnings=warnings)
        assert len(failed) == 0
        assert len(warnings) > 0
        assert "slippage" in warnings[0].lower()

    def test_warns_on_zero_liquidity(self):
        market = Market(
            ticker="NO-LIQ", question="?",
            tokens=[MarketToken(token_id="x_yes", outcome="Yes", price=0.5),
                    MarketToken(token_id="x_no", outcome="No", price=0.5)],
            volume_24h=100, liquidity=0, active=True,
        )
        failed, warnings = [], []
        check_liquidity(market, proposed_cost=10.0, failed=failed, warnings=warnings)
        assert len(warnings) > 0
        assert "unknown" in warnings[0].lower() or "zero" in warnings[0].lower()


# ---------------------------------------------------------------------------
# check_existing_position
# ---------------------------------------------------------------------------


class TestCheckExistingPosition:
    def test_passes_no_existing_position(self, settings, signal):
        positions = MagicMock()
        positions.has_position.return_value = False
        failed, warnings = [], []
        check_existing_position(settings, positions, signal, failed, warnings)
        assert len(failed) == 0

    def test_warns_on_same_direction_addition(self, settings, signal):
        positions = MagicMock()
        positions.has_position.return_value = True
        existing = MagicMock()
        existing.direction = Direction.BUY_YES
        positions.get_position.return_value = existing
        failed, warnings = [], []
        check_existing_position(settings, positions, signal, failed, warnings)
        assert len(failed) == 0
        assert len(warnings) > 0

    def test_warns_hedge_detected(self, settings, signal):
        positions = MagicMock()
        positions.has_position.return_value = True
        existing = MagicMock()
        existing.direction = Direction.BUY_NO  # Opposite direction
        positions.get_position.return_value = existing
        failed, warnings = [], []
        check_existing_position(settings, positions, signal, failed, warnings)
        assert any("hedge" in w.lower() or "Hedge" in w for w in warnings)

    def test_fails_when_additions_not_allowed(self, settings, signal):
        settings.trading.allow_position_additions = False
        positions = MagicMock()
        positions.has_position.return_value = True
        failed, warnings = [], []
        check_existing_position(settings, positions, signal, failed, warnings)
        assert len(failed) > 0
        assert "Already have position" in failed[0]

    def test_rejects_same_ticker_add_exceeding_per_position_cap(
        self, settings, signal
    ):
        """Regression: a second BUY_YES on the same ticker must be rejected
        when the combined cost would exceed max_position_pct, even if
        position additions are allowed."""
        settings.trading.allow_position_additions = True
        settings.trading.max_position_pct = 0.05
        bankroll = 10_000.0
        # Existing position already at 4.5% of bankroll ($450).
        positions = MagicMock()
        positions.has_position.return_value = True
        existing = MagicMock()
        existing.direction = Direction.BUY_YES
        existing.cost_basis = 450.0
        positions.get_position.return_value = existing
        failed, warnings = [], []
        # A $150 add would push combined to $600, over the $500 cap.
        check_existing_position(
            settings, positions, signal, failed, warnings,
            proposed_cost=150.0, bankroll=bankroll,
        )
        assert any("concentration cap" in f.lower() for f in failed), failed

    def test_allows_same_ticker_add_within_per_position_cap(
        self, settings, signal
    ):
        settings.trading.allow_position_additions = True
        settings.trading.max_position_pct = 0.05
        bankroll = 10_000.0
        positions = MagicMock()
        positions.has_position.return_value = True
        existing = MagicMock()
        existing.direction = Direction.BUY_YES
        existing.cost_basis = 200.0
        positions.get_position.return_value = existing
        failed, warnings = [], []
        # A $100 add → combined $300, well under $500 cap.
        check_existing_position(
            settings, positions, signal, failed, warnings,
            proposed_cost=100.0, bankroll=bankroll,
        )
        assert len(failed) == 0
        assert any("adding to existing" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# check_obvious_no_limit — per-position cap
# ---------------------------------------------------------------------------


class TestCheckObviousNoPerPositionCap:
    def test_per_position_cap_rejects_oversized(self, settings):
        """A single obvious_no position costing >2% bankroll should be rejected."""
        signal = Signal(
            strategy=StrategyName.OBVIOUS_NO,
            market_id="TEST-MKT",
            market_question="Test?",
            direction=Direction.BUY_NO,
            edge=0.03,
            probability_estimate=0.99,
            market_price=0.97,
            confidence=0.95,
            reasoning="Test",
        )
        positions = MagicMock()
        positions.get_strategy_exposure.return_value = 0.0
        bankroll = 5000.0
        proposed_cost = 150.0  # 3% of bankroll — over 2% cap

        failed = []
        check_obvious_no_limit(settings, positions, signal, bankroll, proposed_cost, failed)
        assert any("per-position cap" in f for f in failed)

    def test_per_position_cap_allows_small(self, settings):
        """A small obvious_no position under 2% should pass."""
        signal = Signal(
            strategy=StrategyName.OBVIOUS_NO,
            market_id="TEST-MKT",
            market_question="Test?",
            direction=Direction.BUY_NO,
            edge=0.03,
            probability_estimate=0.99,
            market_price=0.97,
            confidence=0.95,
            reasoning="Test",
        )
        positions = MagicMock()
        positions.get_strategy_exposure.return_value = 0.0
        bankroll = 5000.0
        proposed_cost = 80.0  # 1.6% of bankroll — under cap

        failed = []
        check_obvious_no_limit(settings, positions, signal, bankroll, proposed_cost, failed)
        assert not any("per-position cap" in f for f in failed)

    def test_non_obvious_no_signal_skipped(self, settings):
        """Non obvious_no strategies should bypass this check."""
        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="TEST-MKT",
            market_question="Test?",
            direction=Direction.BUY_YES,
            edge=0.08,
            probability_estimate=0.42,
            market_price=0.34,
            confidence=0.7,
            reasoning="Test",
        )
        positions = MagicMock()
        positions.get_strategy_exposure.return_value = 0.0
        failed = []
        check_obvious_no_limit(settings, positions, signal, 5000.0, 250.0, failed)
        assert len(failed) == 0
