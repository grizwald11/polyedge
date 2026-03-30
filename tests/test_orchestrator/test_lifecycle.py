"""Tests for orchestrator lifecycle — _Components, init, strategies, execution, shutdown, trading loop."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Settings
from src.orchestrator.lifecycle import (
    _Components,
    _initialize_services,
    _setup_execution_and_risk,
    _setup_strategies,
    _shutdown,
    run_trading_loop,
)


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture
def settings() -> Settings:
    """Settings with polymarket disabled and no API keys for predictable behavior."""
    s = Settings()
    s.anthropic_api_key = "test-key-123"
    s.kalshi_api_key_id = "test-kalshi-id"
    s.kalshi_private_key_path = "/tmp/fake_key.pem"
    s.polymarket = Settings.model_fields["polymarket"].default_factory()
    s.polymarket.enabled = False
    return s


@pytest.fixture
def logger():
    return logging.getLogger("test.lifecycle")


@pytest.fixture
def components():
    """A fresh _Components instance."""
    return _Components()


# ──────────────────────────────────────────────
# _Components class tests
# ──────────────────────────────────────────────

class TestComponents:
    def test_all_attributes_initialize_to_none(self):
        c = _Components()
        assert c.db is None
        assert c.kalshi is None
        assert c.discovery is None
        assert c.scanner is None
        assert c.forecaster is None
        assert c.calibration is None
        assert c.resolution_tracker is None
        assert c.calibration_analyzer is None
        assert c.data_enricher is None
        assert c.ai_strategy is None
        assert c.no_strategy is None
        assert c.news_strategy is None
        assert c.cross_arb_strategy is None
        assert c.whale_strategy is None
        assert c.market_graph is None
        assert c.portfolio_risk is None
        assert c.poly_scanner is None
        assert c.cross_platform_arb is None
        assert c.polymarket_client is None
        assert c.order_builder is None
        assert c.position_manager is None
        assert c.order_router is None
        assert c.fill_tracker is None
        assert c.alert_manager is None
        assert c.daily_report is None
        assert c.circuit_breaker is None
        assert c.kelly_sizer is None
        assert c.risk_engine is None
        assert c.metrics is None
        assert c.ws_client is None
        assert c.ws_task is None
        assert c.dashboard_task is None

    def test_kalshi_healthy_defaults_to_false(self):
        c = _Components()
        assert c.kalshi_healthy is False


# ──────────────────────────────────────────────
# _initialize_services tests
# ──────────────────────────────────────────────

class TestInitializeServices:
    @pytest.mark.asyncio
    @patch("src.orchestrator.lifecycle.DataEnricher")
    @patch("src.orchestrator.lifecycle.CalibrationAnalyzer")
    @patch("src.orchestrator.lifecycle.ResolutionTracker")
    @patch("src.orchestrator.lifecycle.CalibrationTracker")
    @patch("src.orchestrator.lifecycle.ClaudeForecaster")
    @patch("src.orchestrator.lifecycle.MarketScanner")
    @patch("src.orchestrator.lifecycle.MarketDiscovery")
    @patch("src.orchestrator.lifecycle.KalshiClient")
    @patch("src.orchestrator.lifecycle.Database")
    async def test_returns_populated_components_healthy(
        self, MockDB, MockKalshi, MockDiscovery, MockScanner,
        MockForecaster, MockCalibration, MockResolution, MockCalAnalyzer,
        MockEnricher, settings, logger,
    ):
        mock_kalshi = MockKalshi.return_value
        mock_kalshi.health_check = AsyncMock(return_value=True)
        mock_kalshi.get_balance = AsyncMock(return_value=250.0)

        mock_forecaster = MockForecaster.return_value
        mock_forecaster.health_check = AsyncMock()

        c = await _initialize_services(settings, logger)

        assert c.db is not None
        assert c.kalshi is not None
        assert c.kalshi_healthy is True
        assert c.discovery is not None
        assert c.scanner is not None
        assert c.forecaster is not None
        assert c.calibration is not None
        assert c.resolution_tracker is not None
        assert c.calibration_analyzer is not None
        assert c.data_enricher is not None

    @pytest.mark.asyncio
    @patch("src.orchestrator.lifecycle.DataEnricher")
    @patch("src.orchestrator.lifecycle.CalibrationAnalyzer")
    @patch("src.orchestrator.lifecycle.ResolutionTracker")
    @patch("src.orchestrator.lifecycle.CalibrationTracker")
    @patch("src.orchestrator.lifecycle.ClaudeForecaster")
    @patch("src.orchestrator.lifecycle.MarketScanner")
    @patch("src.orchestrator.lifecycle.MarketDiscovery")
    @patch("src.orchestrator.lifecycle.KalshiClient")
    @patch("src.orchestrator.lifecycle.Database")
    async def test_kalshi_unhealthy_continues(
        self, MockDB, MockKalshi, MockDiscovery, MockScanner,
        MockForecaster, MockCalibration, MockResolution, MockCalAnalyzer,
        MockEnricher, settings, logger,
    ):
        mock_kalshi = MockKalshi.return_value
        mock_kalshi.health_check = AsyncMock(return_value=False)
        mock_kalshi.get_balance = AsyncMock(return_value=None)

        mock_forecaster = MockForecaster.return_value
        mock_forecaster.health_check = AsyncMock()

        c = await _initialize_services(settings, logger)

        assert c.kalshi_healthy is False
        # Other components still created
        assert c.scanner is not None
        assert c.forecaster is not None

    @pytest.mark.asyncio
    @patch("src.orchestrator.lifecycle.DataEnricher")
    @patch("src.orchestrator.lifecycle.CalibrationAnalyzer")
    @patch("src.orchestrator.lifecycle.ResolutionTracker")
    @patch("src.orchestrator.lifecycle.CalibrationTracker")
    @patch("src.orchestrator.lifecycle.ClaudeForecaster")
    @patch("src.orchestrator.lifecycle.MarketScanner")
    @patch("src.orchestrator.lifecycle.MarketDiscovery")
    @patch("src.orchestrator.lifecycle.KalshiClient")
    @patch("src.orchestrator.lifecycle.Database")
    async def test_missing_anthropic_key(
        self, MockDB, MockKalshi, MockDiscovery, MockScanner,
        MockForecaster, MockCalibration, MockResolution, MockCalAnalyzer,
        MockEnricher, settings, logger,
    ):
        settings.anthropic_api_key = None

        mock_kalshi = MockKalshi.return_value
        mock_kalshi.health_check = AsyncMock(return_value=True)
        mock_kalshi.get_balance = AsyncMock(return_value=100.0)

        mock_forecaster = MockForecaster.return_value
        mock_forecaster.health_check = AsyncMock()

        c = await _initialize_services(settings, logger)

        # Forecaster still created, but health_check NOT called (no API key)
        assert c.forecaster is not None
        mock_forecaster.health_check.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("src.orchestrator.lifecycle.DataEnricher")
    @patch("src.orchestrator.lifecycle.CalibrationAnalyzer")
    @patch("src.orchestrator.lifecycle.ResolutionTracker")
    @patch("src.orchestrator.lifecycle.CalibrationTracker")
    @patch("src.orchestrator.lifecycle.ClaudeForecaster")
    @patch("src.orchestrator.lifecycle.MarketScanner")
    @patch("src.orchestrator.lifecycle.MarketDiscovery")
    @patch("src.orchestrator.lifecycle.KalshiClient")
    @patch("src.orchestrator.lifecycle.Database")
    async def test_anthropic_health_check_failure_does_not_crash(
        self, MockDB, MockKalshi, MockDiscovery, MockScanner,
        MockForecaster, MockCalibration, MockResolution, MockCalAnalyzer,
        MockEnricher, settings, logger,
    ):
        mock_kalshi = MockKalshi.return_value
        mock_kalshi.health_check = AsyncMock(return_value=True)
        mock_kalshi.get_balance = AsyncMock(return_value=100.0)

        mock_forecaster = MockForecaster.return_value
        mock_forecaster.health_check = AsyncMock(side_effect=RuntimeError("API down"))

        c = await _initialize_services(settings, logger)

        # Should not crash, forecaster still returned
        assert c.forecaster is not None
        assert c.calibration is not None


# ──────────────────────────────────────────────
# _setup_strategies tests
# ──────────────────────────────────────────────

class TestSetupStrategies:
    @pytest.fixture
    def base_components(self):
        c = _Components()
        c.forecaster = MagicMock()
        c.db = MagicMock()
        c.calibration_analyzer = MagicMock()
        c.data_enricher = MagicMock()
        c.resolution_tracker = MagicMock()
        return c

    @pytest.mark.asyncio
    @patch("src.orchestrator.lifecycle.WhaleMonitor")
    @patch("src.orchestrator.lifecycle.CrossArbStrategy")
    @patch("src.orchestrator.lifecycle.MarketGraph")
    @patch("src.orchestrator.lifecycle.NewsReactiveStrategy")
    @patch("src.orchestrator.lifecycle.NewsIngestion")
    @patch("src.orchestrator.lifecycle.ObviousNoStrategy")
    @patch("src.orchestrator.lifecycle.AIProbabilityStrategy")
    async def test_core_strategies_always_created(
        self, MockAI, MockNO, MockNewsIng, MockNewsStrat,
        MockGraph, MockCrossArb, MockWhale,
        base_components, settings, logger,
    ):
        MockWhale.return_value.basket_size = 0  # No whales

        await _setup_strategies(settings, base_components, logger)

        assert base_components.ai_strategy is not None
        assert base_components.no_strategy is not None
        MockAI.assert_called_once()
        MockNO.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.orchestrator.lifecycle.WhaleMonitor", side_effect=RuntimeError("no whales"))
    @patch("src.orchestrator.lifecycle.CrossArbStrategy", side_effect=RuntimeError("no chromadb"))
    @patch("src.orchestrator.lifecycle.MarketGraph", side_effect=RuntimeError("no chromadb"))
    @patch("src.orchestrator.lifecycle.NewsIngestion", side_effect=RuntimeError("no feeds"))
    @patch("src.orchestrator.lifecycle.ObviousNoStrategy")
    @patch("src.orchestrator.lifecycle.AIProbabilityStrategy")
    async def test_optional_strategies_fail_gracefully(
        self, MockAI, MockNO, MockNewsIng, MockGraph,
        MockCrossArb, MockWhale,
        base_components, settings, logger,
    ):
        await _setup_strategies(settings, base_components, logger)

        # Core still created
        assert base_components.ai_strategy is not None
        assert base_components.no_strategy is not None
        # Optional ones are None
        assert base_components.news_strategy is None
        assert base_components.cross_arb_strategy is None
        assert base_components.whale_strategy is None

    @pytest.mark.asyncio
    @patch("src.orchestrator.lifecycle.WhaleMonitor")
    @patch("src.orchestrator.lifecycle.CrossArbStrategy")
    @patch("src.orchestrator.lifecycle.MarketGraph")
    @patch("src.orchestrator.lifecycle.NewsReactiveStrategy")
    @patch("src.orchestrator.lifecycle.NewsIngestion")
    @patch("src.orchestrator.lifecycle.ObviousNoStrategy")
    @patch("src.orchestrator.lifecycle.AIProbabilityStrategy")
    async def test_polymarket_skipped_when_disabled(
        self, MockAI, MockNO, MockNewsIng, MockNewsStrat,
        MockGraph, MockCrossArb, MockWhale,
        base_components, settings, logger,
    ):
        settings.polymarket.enabled = False
        MockWhale.return_value.basket_size = 0

        await _setup_strategies(settings, base_components, logger)

        assert base_components.poly_scanner is None
        assert base_components.cross_platform_arb is None
        assert base_components.polymarket_client is None

    @pytest.mark.asyncio
    @patch("src.orchestrator.lifecycle.WhaleMonitor")
    @patch("src.orchestrator.lifecycle.CrossArbStrategy")
    @patch("src.orchestrator.lifecycle.MarketGraph")
    @patch("src.orchestrator.lifecycle.NewsReactiveStrategy")
    @patch("src.orchestrator.lifecycle.NewsIngestion")
    @patch("src.orchestrator.lifecycle.ObviousNoStrategy")
    @patch("src.orchestrator.lifecycle.AIProbabilityStrategy")
    async def test_whale_tracker_disabled_with_empty_basket(
        self, MockAI, MockNO, MockNewsIng, MockNewsStrat,
        MockGraph, MockCrossArb, MockWhale,
        base_components, settings, logger,
    ):
        MockWhale.return_value.basket_size = 0

        await _setup_strategies(settings, base_components, logger)

        assert base_components.whale_strategy is None


# ──────────────────────────────────────────────
# _setup_execution_and_risk tests
# ──────────────────────────────────────────────

class TestSetupExecutionAndRisk:
    @pytest.fixture
    def exec_components(self):
        c = _Components()
        c.db = MagicMock()
        c.kalshi = AsyncMock()
        c.kalshi_healthy = False
        c.polymarket_client = None
        return c

    @pytest.mark.asyncio
    @patch("src.orchestrator.lifecycle.Metrics")
    @patch("src.orchestrator.lifecycle.RiskEngine")
    @patch("src.orchestrator.lifecycle.KellySizer")
    @patch("src.orchestrator.lifecycle.CircuitBreaker")
    @patch("src.orchestrator.lifecycle.DailyReport")
    @patch("src.orchestrator.lifecycle.AlertManager")
    @patch("src.orchestrator.lifecycle.PortfolioRisk")
    @patch("src.orchestrator.lifecycle.FillTracker")
    @patch("src.orchestrator.lifecycle.OrderRouter")
    @patch("src.orchestrator.lifecycle.PositionManager")
    @patch("src.orchestrator.lifecycle.OrderBuilder")
    async def test_creates_all_execution_components(
        self, MockOB, MockPM, MockOR, MockFT, MockPR,
        MockAM, MockDR, MockCB, MockKS, MockRE, MockMetrics,
        exec_components, settings, logger,
    ):
        mock_risk = MockRE.return_value
        mock_risk.restore_bankroll = MagicMock()

        await _setup_execution_and_risk(settings, exec_components, logger)

        assert exec_components.order_builder is not None
        assert exec_components.position_manager is not None
        assert exec_components.order_router is not None
        assert exec_components.fill_tracker is not None
        assert exec_components.alert_manager is not None
        assert exec_components.daily_report is not None
        assert exec_components.circuit_breaker is not None
        assert exec_components.kelly_sizer is not None
        assert exec_components.risk_engine is not None
        assert exec_components.metrics is not None

    @pytest.mark.asyncio
    @patch("src.orchestrator.lifecycle.Metrics")
    @patch("src.orchestrator.lifecycle.RiskEngine")
    @patch("src.orchestrator.lifecycle.KellySizer")
    @patch("src.orchestrator.lifecycle.CircuitBreaker")
    @patch("src.orchestrator.lifecycle.DailyReport")
    @patch("src.orchestrator.lifecycle.AlertManager")
    @patch("src.orchestrator.lifecycle.PortfolioRisk", side_effect=RuntimeError("fail"))
    @patch("src.orchestrator.lifecycle.FillTracker")
    @patch("src.orchestrator.lifecycle.OrderRouter")
    @patch("src.orchestrator.lifecycle.PositionManager")
    @patch("src.orchestrator.lifecycle.OrderBuilder")
    async def test_portfolio_risk_failure_does_not_crash(
        self, MockOB, MockPM, MockOR, MockFT, MockPR,
        MockAM, MockDR, MockCB, MockKS, MockRE, MockMetrics,
        exec_components, settings, logger,
    ):
        mock_risk = MockRE.return_value
        mock_risk.restore_bankroll = MagicMock()

        await _setup_execution_and_risk(settings, exec_components, logger)

        assert exec_components.portfolio_risk is None
        # Other components still created
        assert exec_components.risk_engine is not None

    @pytest.mark.asyncio
    @patch("src.orchestrator.lifecycle.Metrics")
    @patch("src.orchestrator.lifecycle.RiskEngine")
    @patch("src.orchestrator.lifecycle.KellySizer")
    @patch("src.orchestrator.lifecycle.CircuitBreaker")
    @patch("src.orchestrator.lifecycle.DailyReport")
    @patch("src.orchestrator.lifecycle.AlertManager")
    @patch("src.orchestrator.lifecycle.PortfolioRisk")
    @patch("src.orchestrator.lifecycle.FillTracker")
    @patch("src.orchestrator.lifecycle.OrderRouter")
    @patch("src.orchestrator.lifecycle.PositionManager")
    @patch("src.orchestrator.lifecycle.OrderBuilder")
    async def test_risk_engine_bankroll_restored(
        self, MockOB, MockPM, MockOR, MockFT, MockPR,
        MockAM, MockDR, MockCB, MockKS, MockRE, MockMetrics,
        exec_components, settings, logger,
    ):
        mock_risk = MockRE.return_value
        mock_risk.restore_bankroll = MagicMock()

        await _setup_execution_and_risk(settings, exec_components, logger)

        mock_risk.restore_bankroll.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.orchestrator.lifecycle.Metrics")
    @patch("src.orchestrator.lifecycle.RiskEngine")
    @patch("src.orchestrator.lifecycle.KellySizer")
    @patch("src.orchestrator.lifecycle.CircuitBreaker")
    @patch("src.orchestrator.lifecycle.DailyReport")
    @patch("src.orchestrator.lifecycle.AlertManager")
    @patch("src.orchestrator.lifecycle.PortfolioRisk")
    @patch("src.orchestrator.lifecycle.FillTracker")
    @patch("src.orchestrator.lifecycle.OrderRouter")
    @patch("src.orchestrator.lifecycle.PositionManager")
    @patch("src.orchestrator.lifecycle.OrderBuilder")
    async def test_alert_manager_configured(
        self, MockOB, MockPM, MockOR, MockFT, MockPR,
        MockAM, MockDR, MockCB, MockKS, MockRE, MockMetrics,
        exec_components, settings, logger,
    ):
        mock_am = MockAM.return_value
        mock_am.register = MagicMock()
        mock_risk = MockRE.return_value
        mock_risk.restore_bankroll = MagicMock()

        await _setup_execution_and_risk(settings, exec_components, logger)

        # LogBackend always registered
        mock_am.register.assert_called()


# ──────────────────────────────────────────────
# _shutdown tests
# ──────────────────────────────────────────────

class TestShutdown:
    @pytest.mark.asyncio
    async def test_all_close_called(self, logger):
        c = _Components()
        c.ws_client = AsyncMock()
        c.ws_client.close = AsyncMock()
        c.ws_task = asyncio.ensure_future(asyncio.sleep(100))
        c.dashboard_task = asyncio.ensure_future(asyncio.sleep(100))
        c.forecaster = AsyncMock()
        c.forecaster.close = AsyncMock()
        c.discovery = AsyncMock()
        c.discovery.close = AsyncMock()
        c.kalshi = AsyncMock()
        c.kalshi.close = AsyncMock()
        c.polymarket_client = AsyncMock()
        c.polymarket_client.close = AsyncMock()
        c.db = MagicMock()
        c.db.close = MagicMock()

        await _shutdown(c, logger)

        c.ws_client.close.assert_awaited_once()
        c.forecaster.close.assert_awaited_once()
        c.discovery.close.assert_awaited_once()
        c.kalshi.close.assert_awaited_once()
        c.polymarket_client.close.assert_awaited_once()
        c.db.close.assert_called_once()
        assert c.ws_task.cancelled()
        assert c.dashboard_task.cancelled()

    @pytest.mark.asyncio
    async def test_one_failure_does_not_prevent_others(self, logger):
        c = _Components()
        c.ws_client = AsyncMock()
        c.ws_client.close = AsyncMock(side_effect=RuntimeError("ws fail"))
        c.ws_task = None
        c.dashboard_task = None
        c.forecaster = AsyncMock()
        c.forecaster.close = AsyncMock(side_effect=RuntimeError("forecaster fail"))
        c.discovery = AsyncMock()
        c.discovery.close = AsyncMock()
        c.kalshi = AsyncMock()
        c.kalshi.close = AsyncMock()
        c.polymarket_client = None
        c.db = MagicMock()
        c.db.close = MagicMock()

        # Should not raise
        await _shutdown(c, logger)

        # Later components still closed despite earlier failures
        c.discovery.close.assert_awaited_once()
        c.kalshi.close.assert_awaited_once()
        c.db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_works_with_all_none_components(self, logger):
        c = _Components()
        # Everything is None by default -- should not raise
        await _shutdown(c, logger)


# ──────────────────────────────────────────────
# run_trading_loop tests
# ──────────────────────────────────────────────

class TestRunTradingLoop:
    def _make_loop_kwargs(self, shutdown_event):
        """Build the kwargs dict for run_trading_loop with all mocks."""
        scanner = MagicMock()
        scanner.db = MagicMock()
        scanner.db.get_daily_pnl = MagicMock(return_value=0.0)
        scanner.db.get_stats = MagicMock(return_value={
            "active_markets": 10,
            "total_signals": 5,
            "total_trades": 2,
            "total_pnl": 15.0,
        })
        scanner.db.cleanup_old_snapshots = MagicMock()
        scanner.db.cleanup_orphaned_records = MagicMock()

        position_manager = MagicMock()
        position_manager.get_total_unrealized_pnl = MagicMock(return_value=0.0)

        circuit_breaker = MagicMock()
        circuit_breaker.record_daily_result = MagicMock()
        circuit_breaker.reset_daily = MagicMock()
        circuit_breaker.trigger_halt = MagicMock()

        daily_report = AsyncMock()
        daily_report.generate_and_send = AsyncMock()

        settings = MagicMock()
        settings.scanning.interval_seconds = 1
        settings.alerts.enabled = False
        settings.alerts.daily_report_time = "21:00"
        settings.execution.cycle_timeout_seconds = 5
        settings.trading.mode = "paper"
        settings.database.snapshot_retention_days = 30

        return dict(
            scanner=scanner,
            kalshi=AsyncMock(),
            ai_strategy=MagicMock(),
            no_strategy=MagicMock(),
            news_strategy=None,
            cross_arb_strategy=None,
            whale_strategy=None,
            market_graph=None,
            risk_engine=MagicMock(),
            kelly_sizer=MagicMock(),
            circuit_breaker=circuit_breaker,
            order_builder=MagicMock(),
            order_router=MagicMock(),
            position_manager=position_manager,
            calibration=MagicMock(),
            resolution_tracker=MagicMock(),
            calibration_analyzer=MagicMock(),
            fill_tracker=MagicMock(),
            alert_manager=AsyncMock(),
            daily_report=daily_report,
            metrics=MagicMock(),
            settings=settings,
            interval=0.1,
            shutdown_event=shutdown_event,
        )

    @pytest.mark.asyncio
    async def test_loop_exits_on_shutdown_event(self):
        shutdown_event = asyncio.Event()
        kwargs = self._make_loop_kwargs(shutdown_event)

        # Set shutdown after a brief delay so the loop runs at least once
        async def _set_shutdown():
            await asyncio.sleep(0.05)
            shutdown_event.set()

        with patch("src.orchestrator.lifecycle.scan_and_trade", new_callable=AsyncMock):
            task = asyncio.create_task(run_trading_loop(**kwargs))
            await _set_shutdown()
            await asyncio.wait_for(task, timeout=5.0)
            # If we get here, the loop exited cleanly

    @pytest.mark.asyncio
    async def test_timeout_handling(self):
        """Test that scan cycle timeout is caught and loop continues."""
        shutdown_event = asyncio.Event()
        kwargs = self._make_loop_kwargs(shutdown_event)
        call_count = 0

        async def slow_scan(*args, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: simulate timeout by sleeping longer than cycle_timeout
                await asyncio.sleep(100)
            else:
                # Second call: set shutdown so loop exits
                shutdown_event.set()

        kwargs["settings"].execution.cycle_timeout_seconds = 0.1

        with patch("src.orchestrator.lifecycle.scan_and_trade", side_effect=slow_scan):
            await asyncio.wait_for(run_trading_loop(**kwargs), timeout=5.0)

        # Loop survived the timeout and ran at least one more cycle
        assert call_count >= 1

    @pytest.mark.asyncio
    async def test_general_exception_does_not_crash_loop(self):
        """Test that a generic exception in scan_and_trade does not kill the loop."""
        shutdown_event = asyncio.Event()
        kwargs = self._make_loop_kwargs(shutdown_event)
        call_count = 0

        async def failing_scan(*args, **kw):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ValueError("something broke")
            shutdown_event.set()

        with patch("src.orchestrator.lifecycle.scan_and_trade", side_effect=failing_scan):
            await asyncio.wait_for(run_trading_loop(**kwargs), timeout=5.0)

        assert call_count >= 2
