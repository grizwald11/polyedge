"""Tests for main orchestrator — scan_and_trade cycle logic."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Settings
from src.core.models import (
    Direction,
    Market,
    MarketCategory,
    MarketToken,
    Order,
    OrderStatus,
    OrderType,
    Side,
    Signal,
    StrategyName,
    Trade,
)
from src.main import scan_and_trade, setup_logging

# ──────────────────────────────────────────────
# Fixtures — mock every component scan_and_trade needs
# ──────────────────────────────────────────────

@pytest.fixture
def settings():
    s = Settings()
    s.trading.mode = "paper"
    s.trading.bankroll = 500.0
    s.alerts.alert_on_trade = False
    s.alerts.alert_on_circuit_breaker = False
    return s


@pytest.fixture
def sample_markets():
    return [
        Market(
            ticker="MKT-A",
            question="Will A happen?",
            category=MarketCategory.POLITICS,
            tokens=[
                MarketToken(token_id="MKT-A_yes", outcome="Yes", price=0.40),
                MarketToken(token_id="MKT-A_no", outcome="No", price=0.60),
            ],
            volume_24h=50000,
            liquidity=20000,
            active=True,
        ),
    ]


@pytest.fixture
def components(sample_markets):
    """Create mocked versions of all components needed by scan_and_trade."""
    scanner = MagicMock()
    scanner.run_scan_cycle = AsyncMock(return_value=sample_markets)
    scanner.db = MagicMock()
    scanner.db.log_signal.return_value = 1
    scanner.db.get_stats.return_value = {
        "active_markets": 10, "total_signals": 5, "total_trades": 2, "total_pnl": 10.0,
    }

    ai_strategy = MagicMock()
    ai_strategy.scan_for_opportunities = AsyncMock(return_value=[])

    no_strategy = MagicMock()
    no_strategy.scan_for_opportunities.return_value = []

    risk_engine = MagicMock()
    risk_result = MagicMock()
    risk_result.passed = True
    risk_result.failed_checks = []
    risk_engine.check_all.return_value = risk_result

    kelly_sizer = MagicMock()
    kelly_sizer.calculate_position_size.return_value = 5

    circuit_breaker = MagicMock()
    circuit_breaker.check.return_value = True
    circuit_breaker.get_kelly_multiplier.return_value = 1.0

    order_builder = MagicMock()
    order = Order(
        id="PE-test-1", market_id="MKT-A", token_id="MKT-A_yes",
        side=Side.BUY, price=0.40, size=5, cost=2.0,
        order_type=OrderType.GTC, status=OrderStatus.PENDING,
        strategy=StrategyName.AI_PROBABILITY, paper=True,
    )
    order_builder.build_limit_order.return_value = order
    order_builder.build_market_order.return_value = order
    order_builder._generate_order_id.return_value = "PE-exit-1"

    trade = Trade(
        order_id="PE-test-1", market_id="MKT-A", token_id="MKT-A_yes",
        side=Side.BUY, price=0.40, size=5, fee=0.01,
        strategy=StrategyName.AI_PROBABILITY, paper=True,
    )
    route_result = MagicMock()
    route_result.success = True
    route_result.trade = trade
    order_router = MagicMock()
    order_router.route_order = AsyncMock(return_value=route_result)
    order_router.cancel_stale_orders = AsyncMock(return_value=0)

    position_manager = MagicMock()
    position_manager.get_total_exposure.return_value = 0.0
    position_manager.get_total_unrealized_pnl.return_value = 0.0
    position_manager.get_position_count.return_value = 0
    position_manager.get_all_positions.return_value = []
    position_manager.get_exit_candidates.return_value = []

    calibration = MagicMock()
    resolution_tracker = MagicMock()
    resolution_tracker.check_resolutions = AsyncMock(return_value=0)
    calibration_analyzer = MagicMock()
    fill_tracker = MagicMock()
    fill_tracker.check_fills = AsyncMock(return_value=[])
    alert_manager = MagicMock()
    alert_manager.send_trade_alert = AsyncMock()
    alert_manager.send_circuit_breaker_alert = AsyncMock()
    metrics = MagicMock()

    # Mock kalshi client for position price fetches
    kalshi = AsyncMock()
    kalshi.get_market = AsyncMock(return_value=None)
    # check_key_freshness is called synchronously (no await) in production code,
    # so it must be a regular MagicMock to avoid unawaited coroutine warnings.
    kalshi.check_key_freshness = MagicMock(return_value=True)

    # Mock DB dedup check
    scanner.db.has_recent_trade = MagicMock(return_value=False)

    return {
        "scanner": scanner,
        "kalshi": kalshi,
        "ai_strategy": ai_strategy,
        "no_strategy": no_strategy,
        "news_strategy": None,
        "cross_arb_strategy": None,
        "whale_strategy": None,
        "market_graph": None,
        "risk_engine": risk_engine,
        "kelly_sizer": kelly_sizer,
        "circuit_breaker": circuit_breaker,
        "order_builder": order_builder,
        "order_router": order_router,
        "position_manager": position_manager,
        "calibration": calibration,
        "resolution_tracker": resolution_tracker,
        "calibration_analyzer": calibration_analyzer,
        "fill_tracker": fill_tracker,
        "alert_manager": alert_manager,
        "metrics": metrics,
        "settings": None,  # Replaced per-test
    }


# ──────────────────────────────────────────────
# setup_logging
# ──────────────────────────────────────────────

class TestSetupLogging:
    def test_creates_log_directory(self, tmp_path):
        log_file = str(tmp_path / "subdir" / "test.log")
        setup_logging("DEBUG", log_file)
        assert os.path.isdir(str(tmp_path / "subdir"))
        # Cleanup handlers to avoid test pollution
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)

    def test_sets_log_level(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        setup_logging("WARNING", log_file)
        root = logging.getLogger()
        assert root.level == logging.WARNING
        for h in root.handlers[:]:
            root.removeHandler(h)


# ──────────────────────────────────────────────
# scan_and_trade — core cycle logic
# ──────────────────────────────────────────────

class TestScanAndTrade:
    @pytest.mark.asyncio
    async def test_no_markets_returns_early(self, settings, components):
        components["scanner"].run_scan_cycle = AsyncMock(return_value=[])
        components["settings"] = settings
        await scan_and_trade(**components)
        # Should not attempt to generate signals
        components["ai_strategy"].scan_for_opportunities.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_signals_records_metrics(self, settings, components):
        components["settings"] = settings
        await scan_and_trade(**components)
        # AI + NO strategies produce no signals → metrics recorded with 0 trades
        components["metrics"].record_cycle.assert_called_once()
        call_kwargs = components["metrics"].record_cycle.call_args
        assert call_kwargs[1]["trades"] == 0 or call_kwargs[0][1] == 0

    @pytest.mark.asyncio
    async def test_circuit_breaker_halts_cycle(self, settings, components):
        components["circuit_breaker"].check.return_value = False
        components["circuit_breaker"].halt_reason = "Daily loss limit"
        components["settings"] = settings
        await scan_and_trade(**components)
        # Should not scan for markets
        components["scanner"].run_scan_cycle.assert_not_called()

    @pytest.mark.asyncio
    async def test_circuit_breaker_sends_alert(self, settings, components):
        settings.alerts.alert_on_circuit_breaker = True
        components["circuit_breaker"].check.return_value = False
        components["circuit_breaker"].halt_reason = "Daily loss limit"
        components["settings"] = settings
        await scan_and_trade(**components)
        components["alert_manager"].send_circuit_breaker_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_signal_generates_trade(self, settings, components):
        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="MKT-A",
            market_question="Will A happen?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.50,
            market_price=0.40,
            confidence=0.7,
            reasoning="Test",
        )
        components["ai_strategy"].scan_for_opportunities = AsyncMock(return_value=[signal])
        components["settings"] = settings
        await scan_and_trade(**components)
        # Should have routed an order
        components["order_router"].route_order.assert_called_once()
        # Should have updated position manager
        components["position_manager"].update_from_trade.assert_called()

    @pytest.mark.asyncio
    async def test_risk_check_blocks_trade(self, settings, components):
        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="MKT-A",
            market_question="Will A happen?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.50,
            market_price=0.40,
            confidence=0.7,
            reasoning="Test",
        )
        components["ai_strategy"].scan_for_opportunities = AsyncMock(return_value=[signal])
        risk_result = MagicMock()
        risk_result.passed = False
        risk_result.failed_checks = ["position_limit"]
        components["risk_engine"].check_all.return_value = risk_result
        components["settings"] = settings
        await scan_and_trade(**components)
        # Order should NOT have been routed
        components["order_router"].route_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_trades_per_cycle_enforced(self, settings, components):
        settings.trading.max_trades_per_cycle = 1
        signals = [
            Signal(
                strategy=StrategyName.AI_PROBABILITY,
                market_id=f"MKT-{i}",
                market_question=f"Will {i} happen?",
                direction=Direction.BUY_YES,
                edge=0.10,
                probability_estimate=0.50,
                market_price=0.40,
                confidence=0.7,
                reasoning="Test",
            )
            for i in range(3)
        ]
        components["ai_strategy"].scan_for_opportunities = AsyncMock(return_value=signals)
        # Need markets for all signals
        from src.core.models import Market, MarketCategory, MarketToken
        markets = [
            Market(
                ticker=f"MKT-{i}",
                question=f"Will {i} happen?",
                category=MarketCategory.POLITICS,
                tokens=[
                    MarketToken(token_id=f"MKT-{i}_yes", outcome="Yes", price=0.40),
                    MarketToken(token_id=f"MKT-{i}_no", outcome="No", price=0.60),
                ],
                volume_24h=50000, liquidity=20000, active=True,
            )
            for i in range(3)
        ]
        components["scanner"].run_scan_cycle = AsyncMock(return_value=markets)
        components["settings"] = settings
        await scan_and_trade(**components)
        # Only 1 trade should execute
        assert components["order_router"].route_order.call_count == 1

    @pytest.mark.asyncio
    async def test_duplicate_market_signals_deduped(self, settings, components):
        """Contradictory signals (BUY_YES vs BUY_NO) for the same market
        are skipped entirely by signal deconfliction (C-10)."""
        signals = [
            Signal(
                strategy=StrategyName.AI_PROBABILITY,
                market_id="MKT-A",
                market_question="Will A happen?",
                direction=Direction.BUY_YES,
                edge=0.10,
                probability_estimate=0.50,
                market_price=0.40,
                confidence=0.7,
                reasoning="Test 1",
            ),
            Signal(
                strategy=StrategyName.OBVIOUS_NO,
                market_id="MKT-A",  # Same market
                market_question="Will A happen?",
                direction=Direction.BUY_NO,
                edge=0.05,
                probability_estimate=0.50,
                market_price=0.40,
                confidence=0.5,
                reasoning="Test 2",
            ),
        ]
        components["ai_strategy"].scan_for_opportunities = AsyncMock(return_value=signals[:1])
        components["no_strategy"].scan_for_opportunities.return_value = signals[1:]
        components["settings"] = settings
        await scan_and_trade(**components)
        # Contradictory signals for same market are skipped entirely
        assert components["order_router"].route_order.call_count == 0

    @pytest.mark.asyncio
    async def test_fill_tracker_checked_first(self, settings, components):
        fill = Trade(
            order_id="PE-old", market_id="MKT-OLD", token_id="MKT-OLD_yes",
            side=Side.BUY, price=0.50, size=10, fee=0.01,
            strategy=StrategyName.AI_PROBABILITY, paper=False,
        )
        components["fill_tracker"].check_fills = AsyncMock(return_value=[fill])
        components["settings"] = settings
        await scan_and_trade(**components)
        components["fill_tracker"].check_fills.assert_called_once()
        components["position_manager"].update_from_trade.assert_any_call(fill)

    @pytest.mark.asyncio
    async def test_scan_failure_records_error(self, settings, components):
        components["scanner"].run_scan_cycle = AsyncMock(side_effect=Exception("API down"))
        components["settings"] = settings
        await scan_and_trade(**components)
        components["metrics"].record_error.assert_called_once_with("scanner", "API down")

    @pytest.mark.asyncio
    async def test_kelly_zero_contracts_skips(self, settings, components):
        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="MKT-A",
            market_question="Will A happen?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.50,
            market_price=0.40,
            confidence=0.7,
            reasoning="Test",
        )
        components["ai_strategy"].scan_for_opportunities = AsyncMock(return_value=[signal])
        components["kelly_sizer"].calculate_position_size.return_value = 0
        components["settings"] = settings
        await scan_and_trade(**components)
        components["order_router"].route_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_calibration_report_on_cycle_10(self, settings, components):
        # Need at least one signal so the cycle doesn't return early
        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="MKT-A",
            market_question="Will A happen?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.50,
            market_price=0.40,
            confidence=0.7,
            reasoning="Test",
        )
        components["ai_strategy"].scan_for_opportunities = AsyncMock(return_value=[signal])
        components["settings"] = settings
        report = MagicMock()
        report.total_resolved = 20
        report.overall_brier = 0.15
        report.overall_win_rate = 0.60
        report.total_unresolved = 5
        report.best_category = "Politics"
        report.worst_category = "Tech"
        components["calibration_analyzer"].generate_report.return_value = report
        components["calibration_analyzer"].get_category_adjustments.return_value = {}

        await scan_and_trade(**components, cycle_count=10)
        components["calibration_analyzer"].generate_report.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_calibration_report_on_cycle_7(self, settings, components):
        # Need at least one signal so the cycle doesn't return early before calibration check
        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="MKT-A",
            market_question="Will A happen?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.50,
            market_price=0.40,
            confidence=0.7,
            reasoning="Test",
        )
        components["ai_strategy"].scan_for_opportunities = AsyncMock(return_value=[signal])
        components["settings"] = settings
        await scan_and_trade(**components, cycle_count=7)
        components["calibration_analyzer"].generate_report.assert_not_called()

    @pytest.mark.asyncio
    async def test_optional_strategies_called_when_present(self, settings, components):
        news = MagicMock()
        news.scan_for_opportunities = AsyncMock(return_value=[])
        cross_arb = MagicMock()
        cross_arb.scan_for_opportunities = AsyncMock(return_value=[])
        whale = MagicMock()
        whale.scan_for_opportunities.return_value = []

        components["news_strategy"] = news
        components["cross_arb_strategy"] = cross_arb
        components["whale_strategy"] = whale
        components["settings"] = settings

        await scan_and_trade(**components)

        news.scan_for_opportunities.assert_called_once()
        cross_arb.scan_for_opportunities.assert_called_once()
        whale.scan_for_opportunities.assert_called_once()

    @pytest.mark.asyncio
    async def test_trade_sends_alert(self, settings, components):
        settings.alerts.alert_on_trade = True
        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="MKT-A",
            market_question="Will A happen?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.50,
            market_price=0.40,
            confidence=0.7,
            reasoning="Test",
        )
        components["ai_strategy"].scan_for_opportunities = AsyncMock(return_value=[signal])
        components["settings"] = settings
        await scan_and_trade(**components)
        components["alert_manager"].send_trade_alert.assert_called_once()
