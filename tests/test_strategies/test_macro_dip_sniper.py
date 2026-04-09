"""Tests for the Macro Dip Sniper strategy."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.analysis.regime_detector import (
    REGIME_MULTIPLIERS,
    MarketRegime,
    RegimeAnalysis,
)
from src.config import Settings
from src.core.models import (
    Direction,
    ForecastResult,
    Market,
    MarketCategory,
    MarketToken,
    StrategyName,
)
from src.strategies.macro_dip_sniper import (
    MAX_CONCURRENT_POSITIONS,
    MacroDipSniperStrategy,
)


def _make_regime(regime: MarketRegime) -> RegimeAnalysis:
    return RegimeAnalysis(
        regime=regime,
        multipliers=REGIME_MULTIPLIERS[regime],
        cross_market_volatility=0.01 if regime == MarketRegime.LOW_VOL else 0.08,
        volume_spike_ratio=1.0,
        markets_analyzed=10,
        detail="synthetic",
    )


def _make_market(
    *,
    ticker: str = "BTC-DIP-58K",
    question: str = "Will BTC dip to $58,000 by April 30?",
    yes_price: float = 0.12,
    days: float = 14.0,
    category: MarketCategory = MarketCategory.CRYPTO,
    volume_24h: float = 50_000.0,
) -> Market:
    end_date = (
        datetime.now(timezone.utc) + timedelta(days=days) if days is not None else None
    )
    return Market(
        ticker=ticker,
        question=question,
        category=category,
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome="Yes", price=yes_price),
            MarketToken(token_id=f"{ticker}_no", outcome="No", price=1.0 - yes_price),
        ],
        end_date=end_date,
        volume_24h=volume_24h,
        liquidity=volume_24h,
        active=True,
    )


def _make_forecast(
    probability: float, parse_failed: bool = False
) -> ForecastResult:
    return ForecastResult(
        probability=probability,
        confidence_low=max(0.0, probability - 0.1),
        confidence_high=min(1.0, probability + 0.1),
        reasoning="synthetic forecast for test",
        model_used="test",
        parse_failed=parse_failed,
    )


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def forecaster_mock() -> MagicMock:
    mock = MagicMock()
    mock.assess_market = AsyncMock(return_value=_make_forecast(0.25))
    return mock


@pytest.fixture
def strategy(settings, forecaster_mock) -> MacroDipSniperStrategy:
    return MacroDipSniperStrategy(settings, forecaster_mock)


# ──────────────────────────────────────────────
# Threshold question detection
# ──────────────────────────────────────────────


class TestThresholdDetection:
    def test_matches_btc_dip_dollar_k(self, strategy):
        assert strategy._is_threshold_question("Will BTC dip to $58k by April?")

    def test_matches_eth_fall_below(self, strategy):
        assert strategy._is_threshold_question(
            "Will ETH fall below $2,500 this week?"
        )

    def test_matches_sp500_drop_under(self, strategy):
        assert strategy._is_threshold_question(
            "Will the S&P 500 drop under 5000 by month-end?"
        )

    def test_matches_crash_plunge(self, strategy):
        assert strategy._is_threshold_question(
            "Will Bitcoin crash to $40,000 by June?"
        )

    def test_rejects_categorical_fed_question(self, strategy):
        assert not strategy._is_threshold_question("Will the Fed cut in May?")

    def test_rejects_plain_election(self, strategy):
        assert not strategy._is_threshold_question(
            "Will the Democrats win the Senate?"
        )

    def test_rejects_keyword_without_number(self, strategy):
        assert not strategy._is_threshold_question(
            "Will the market fall significantly?"
        )

    def test_empty_question(self, strategy):
        assert not strategy._is_threshold_question("")


# ──────────────────────────────────────────────
# Regime gate
# ──────────────────────────────────────────────


class TestRegimeGate:
    @pytest.mark.asyncio
    async def test_fires_only_in_low_vol(self, strategy):
        market = _make_market()
        signals = await strategy.generate_signals(
            [market], regime_analysis=_make_regime(MarketRegime.LOW_VOL),
        )
        assert len(signals) == 1

    @pytest.mark.asyncio
    async def test_blocked_in_normal(self, strategy):
        market = _make_market()
        signals = await strategy.generate_signals(
            [market], regime_analysis=_make_regime(MarketRegime.NORMAL),
        )
        assert signals == []

    @pytest.mark.asyncio
    async def test_blocked_in_high_vol(self, strategy):
        market = _make_market()
        signals = await strategy.generate_signals(
            [market], regime_analysis=_make_regime(MarketRegime.HIGH_VOL),
        )
        assert signals == []

    @pytest.mark.asyncio
    async def test_blocked_in_crisis(self, strategy):
        market = _make_market()
        signals = await strategy.generate_signals(
            [market], regime_analysis=_make_regime(MarketRegime.CRISIS),
        )
        assert signals == []

    @pytest.mark.asyncio
    async def test_blocked_with_no_regime_analysis(self, strategy):
        market = _make_market()
        signals = await strategy.generate_signals([market], regime_analysis=None)
        assert signals == []


# ──────────────────────────────────────────────
# Price & time filters
# ──────────────────────────────────────────────


class TestPriceTimeFilters:
    LOW_VOL = _make_regime(MarketRegime.LOW_VOL)

    @pytest.mark.asyncio
    async def test_rejects_yes_price_too_high(self, strategy):
        market = _make_market(yes_price=0.25)
        signals = await strategy.generate_signals([market], regime_analysis=self.LOW_VOL)
        assert signals == []

    @pytest.mark.asyncio
    async def test_rejects_yes_price_too_low(self, strategy):
        market = _make_market(yes_price=0.01)
        signals = await strategy.generate_signals([market], regime_analysis=self.LOW_VOL)
        assert signals == []

    @pytest.mark.asyncio
    async def test_rejects_resolving_too_soon(self, strategy):
        market = _make_market(days=1.0)
        signals = await strategy.generate_signals([market], regime_analysis=self.LOW_VOL)
        assert signals == []

    @pytest.mark.asyncio
    async def test_rejects_resolving_too_far(self, strategy):
        market = _make_market(days=60.0)
        signals = await strategy.generate_signals([market], regime_analysis=self.LOW_VOL)
        assert signals == []

    @pytest.mark.asyncio
    async def test_rejects_low_volume(self, strategy):
        market = _make_market(volume_24h=100.0)
        signals = await strategy.generate_signals([market], regime_analysis=self.LOW_VOL)
        assert signals == []

    @pytest.mark.asyncio
    async def test_rejects_non_threshold_question(self, strategy):
        market = _make_market(
            question="Will Taylor Swift win album of the year?",
            category=MarketCategory.CULTURE,
        )
        signals = await strategy.generate_signals([market], regime_analysis=self.LOW_VOL)
        assert signals == []

    @pytest.mark.asyncio
    async def test_rejects_disallowed_category(self, strategy):
        market = _make_market(category=MarketCategory.SPORTS)
        signals = await strategy.generate_signals([market], regime_analysis=self.LOW_VOL)
        assert signals == []


# ──────────────────────────────────────────────
# Edge / fee math
# ──────────────────────────────────────────────


class TestEdgeAndFees:
    LOW_VOL = _make_regime(MarketRegime.LOW_VOL)

    @pytest.mark.asyncio
    async def test_signal_edge_uses_forecast_probability(self, settings, forecaster_mock):
        forecaster_mock.assess_market = AsyncMock(return_value=_make_forecast(0.30))
        strat = MacroDipSniperStrategy(settings, forecaster_mock)
        # Use a macro (non-fee) category so edge is clean: 0.30 - 0.12 = 0.18
        market = _make_market(
            question="Will 10-year yields fall below 3.5%?",
            category=MarketCategory.FED_MACRO,
        )
        signals = await strat.generate_signals([market], regime_analysis=self.LOW_VOL)
        assert len(signals) == 1
        assert signals[0].direction == Direction.BUY_YES
        assert signals[0].strategy == StrategyName.MACRO_DIP_SNIPER
        assert signals[0].probability_estimate == pytest.approx(0.30)
        assert signals[0].market_price == pytest.approx(0.12)
        assert signals[0].edge == pytest.approx(0.18, abs=1e-6)

    @pytest.mark.asyncio
    async def test_crypto_edge_is_fee_adjusted(self, settings, forecaster_mock):
        forecaster_mock.assess_market = AsyncMock(return_value=_make_forecast(0.25))
        strat = MacroDipSniperStrategy(settings, forecaster_mock)
        market = _make_market(yes_price=0.12, category=MarketCategory.CRYPTO)
        signals = await strat.generate_signals([market], regime_analysis=self.LOW_VOL)
        assert len(signals) == 1
        # raw edge = 0.25 - 0.12 = 0.13
        # fee = 0.0175 * 0.12 * 0.88 ≈ 0.001848
        # net edge ≈ 0.128152
        expected_fee = round(0.0175 * 0.12 * 0.88, 6)
        expected_edge = 0.13 - expected_fee
        assert signals[0].edge == pytest.approx(expected_edge, abs=1e-6)

    @pytest.mark.asyncio
    async def test_rejects_insufficient_edge(self, settings, forecaster_mock):
        # Forecaster estimates ~= market price → edge below min_edge
        forecaster_mock.assess_market = AsyncMock(return_value=_make_forecast(0.13))
        strat = MacroDipSniperStrategy(settings, forecaster_mock)
        market = _make_market(yes_price=0.12, category=MarketCategory.FED_MACRO)
        signals = await strat.generate_signals([market], regime_analysis=self.LOW_VOL)
        assert signals == []

    @pytest.mark.asyncio
    async def test_skips_failed_forecast(self, settings, forecaster_mock):
        forecaster_mock.assess_market = AsyncMock(
            return_value=_make_forecast(0.30, parse_failed=True)
        )
        strat = MacroDipSniperStrategy(settings, forecaster_mock)
        market = _make_market(category=MarketCategory.FED_MACRO)
        signals = await strat.generate_signals([market], regime_analysis=self.LOW_VOL)
        assert signals == []

    @pytest.mark.asyncio
    async def test_forecaster_exception_is_swallowed(self, settings, forecaster_mock):
        forecaster_mock.assess_market = AsyncMock(side_effect=RuntimeError("boom"))
        strat = MacroDipSniperStrategy(settings, forecaster_mock)
        market = _make_market(category=MarketCategory.FED_MACRO)
        # Should not raise — strategy must be resilient to forecaster failures
        signals = await strat.generate_signals([market], regime_analysis=self.LOW_VOL)
        assert signals == []


# ──────────────────────────────────────────────
# Concurrency cap
# ──────────────────────────────────────────────


class TestConcurrencyCap:
    LOW_VOL = _make_regime(MarketRegime.LOW_VOL)

    @pytest.mark.asyncio
    async def test_cap_blocks_new_signals(self, strategy):
        # Fill up the active set to the cap
        for i in range(MAX_CONCURRENT_POSITIONS):
            strategy.mark_position_opened(f"MKT-{i}")
        market = _make_market()
        signals = await strategy.generate_signals(
            [market], regime_analysis=self.LOW_VOL,
        )
        assert signals == []

    @pytest.mark.asyncio
    async def test_closing_position_allows_new_signal(self, strategy):
        for i in range(MAX_CONCURRENT_POSITIONS):
            strategy.mark_position_opened(f"MKT-{i}")
        strategy.mark_position_closed("MKT-0")
        market = _make_market()
        signals = await strategy.generate_signals(
            [market], regime_analysis=self.LOW_VOL,
        )
        assert len(signals) == 1


# ──────────────────────────────────────────────
# Candidate prioritization
# ──────────────────────────────────────────────


class TestCandidateLimits:
    LOW_VOL = _make_regime(MarketRegime.LOW_VOL)

    @pytest.mark.asyncio
    async def test_cheapest_candidates_assessed_first(self, settings, forecaster_mock):
        # MAX_CANDIDATES_PER_CYCLE = 5 — create 10 candidates at varying prices and
        # confirm only the 5 cheapest get assessed.
        from src.strategies.macro_dip_sniper import MAX_CANDIDATES_PER_CYCLE

        forecaster_mock.assess_market = AsyncMock(return_value=_make_forecast(0.30))
        strat = MacroDipSniperStrategy(settings, forecaster_mock)

        markets = [
            _make_market(
                ticker=f"BTC-DIP-{i}",
                yes_price=0.04 + i * 0.01,  # 0.04, 0.05, ..., 0.13
                category=MarketCategory.FED_MACRO,
                question=f"Will rates fall below {3000 + i * 100}?",
            )
            for i in range(10)
        ]

        await strat.generate_signals(markets, regime_analysis=self.LOW_VOL)
        assert forecaster_mock.assess_market.await_count == MAX_CANDIDATES_PER_CYCLE
