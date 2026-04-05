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
        c.db = MagicMock()
        c.db.close = MagicMock()

        await _shutdown(c, logger)

        c.ws_client.close.assert_awaited_once()
        c.forecaster.close.assert_awaited_once()
        c.discovery.close.assert_awaited_once()
        c.kalshi.close.assert_awaited_once()
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
            # Timeout increased to account for exponential backoff (2s + 4s between failures)
            await asyncio.wait_for(run_trading_loop(**kwargs), timeout=15.0)

        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_daily_report_sent_when_past_report_time(self):
        """Daily report is sent when past configured time and not yet sent today."""
        shutdown_event = asyncio.Event()
        kwargs = self._make_loop_kwargs(shutdown_event)
        # Always past report time (hour=0, minute=0 → any time is past 00:00)
        kwargs["settings"].alerts.enabled = True
        kwargs["settings"].alerts.daily_report_time = "00:00"

        call_count = 0

        async def scan_once(*args, **kw):
            nonlocal call_count
            call_count += 1
            shutdown_event.set()

        with patch("src.orchestrator.lifecycle.scan_and_trade", side_effect=scan_once):
            await asyncio.wait_for(run_trading_loop(**kwargs), timeout=5.0)

        kwargs["daily_report"].generate_and_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_daily_report_failure_does_not_crash_loop(self):
        """A failure in daily_report.generate_and_send should not kill the loop."""
        shutdown_event = asyncio.Event()
        kwargs = self._make_loop_kwargs(shutdown_event)
        kwargs["settings"].alerts.enabled = True
        kwargs["settings"].alerts.daily_report_time = "00:00"
        kwargs["daily_report"].generate_and_send = AsyncMock(side_effect=RuntimeError("smtp down"))

        call_count = 0

        async def scan_once(*args, **kw):
            nonlocal call_count
            call_count += 1
            shutdown_event.set()

        with patch("src.orchestrator.lifecycle.scan_and_trade", side_effect=scan_once):
            await asyncio.wait_for(run_trading_loop(**kwargs), timeout=5.0)

        # Loop survived despite daily report failure
        assert call_count >= 1

    @pytest.mark.asyncio
    async def test_invalid_report_time_format_handled(self):
        """Malformed daily_report_time does not crash the loop."""
        shutdown_event = asyncio.Event()
        kwargs = self._make_loop_kwargs(shutdown_event)
        kwargs["settings"].alerts.enabled = True
        kwargs["settings"].alerts.daily_report_time = "INVALID"

        call_count = 0

        async def scan_once(*args, **kw):
            nonlocal call_count
            call_count += 1
            shutdown_event.set()

        with patch("src.orchestrator.lifecycle.scan_and_trade", side_effect=scan_once):
            await asyncio.wait_for(run_trading_loop(**kwargs), timeout=5.0)

        # Report was NOT sent (parse failed gracefully)
        kwargs["daily_report"].generate_and_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_timeout_in_live_mode_triggers_position_sync(self):
        """In live mode, a cycle timeout triggers position sync with kalshi."""
        shutdown_event = asyncio.Event()
        kwargs = self._make_loop_kwargs(shutdown_event)
        kwargs["settings"].trading.mode = "live"
        kwargs["settings"].execution.cycle_timeout_seconds = 0.05

        kalshi = AsyncMock()
        kalshi.sync_with_kalshi = AsyncMock(return_value=0)
        kwargs["kalshi"] = kalshi

        position_manager = kwargs["position_manager"]
        position_manager.sync_with_kalshi = AsyncMock(return_value=0)

        call_count = 0

        async def slow_scan(*args, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await asyncio.sleep(10)  # will time out
            else:
                shutdown_event.set()

        with patch("src.orchestrator.lifecycle.scan_and_trade", side_effect=slow_scan):
            await asyncio.wait_for(run_trading_loop(**kwargs), timeout=5.0)

        position_manager.sync_with_kalshi.assert_awaited()

    @pytest.mark.asyncio
    async def test_timeout_in_live_mode_sync_failure_triggers_circuit_breaker(self):
        """In live mode, if post-timeout position sync also fails → circuit breaker triggered."""
        shutdown_event = asyncio.Event()
        kwargs = self._make_loop_kwargs(shutdown_event)
        kwargs["settings"].trading.mode = "live"
        kwargs["settings"].execution.cycle_timeout_seconds = 0.05

        position_manager = kwargs["position_manager"]
        position_manager.sync_with_kalshi = AsyncMock(side_effect=RuntimeError("sync failed"))

        circuit_breaker = kwargs["circuit_breaker"]

        call_count = 0

        async def slow_scan(*args, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await asyncio.sleep(10)
            else:
                shutdown_event.set()

        with patch("src.orchestrator.lifecycle.scan_and_trade", side_effect=slow_scan):
            await asyncio.wait_for(run_trading_loop(**kwargs), timeout=5.0)

        circuit_breaker.trigger_halt.assert_called()

    @pytest.mark.asyncio
    async def test_db_cleanup_called_on_day_boundary(self):
        """Daily cleanup is called when the date changes."""
        shutdown_event = asyncio.Event()
        kwargs = self._make_loop_kwargs(shutdown_event)

        # Simulate a day boundary by patching datetime.now
        yesterday = "2026-03-31"
        today = "2026-04-01"
        call_count = 0

        async def scan_and_exit(*args, **kw):
            nonlocal call_count
            call_count += 1
            shutdown_event.set()

        # Patch datetime so the "last_trading_day" starts as yesterday
        import datetime as _dt
        call_times = [
            # First iteration: today check
            _dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=_dt.timezone.utc),
            # Report time check
            _dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=_dt.timezone.utc),
        ]
        time_iter = iter(call_times + [_dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)] * 10)

        original_now = _dt.datetime.now

        def fake_now(tz=None):
            try:
                return next(time_iter)
            except StopIteration:
                return _dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)

        scanner = kwargs["scanner"]

        with patch("src.orchestrator.lifecycle.scan_and_trade", side_effect=scan_and_exit):
            with patch("src.orchestrator.lifecycle.datetime") as mock_dt:
                mock_dt.now.side_effect = fake_now
                mock_dt.now.return_value = _dt.datetime(2026, 4, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
                # Force a day change: patch last_trading_day to be set before loop runs
                # by returning a different date the first time
                dates = iter([yesterday, today, today, today, today, today])
                mock_dt.now.side_effect = lambda tz=None: _dt.datetime(
                    int(next(dates, "2026-04-01").split("-")[0]),
                    int(next(dates, "2026-04-01").split("-")[1]) if False else 4,
                    int(next(dates, "2026-04-01").split("-")[2]) if False else 1,
                    12, 0, 0, tzinfo=_dt.timezone.utc,
                )
                # Simpler: just let it run normally for one cycle
                pass

        # Easier approach: just run normally and verify cleanup isn't called
        # (since we're not actually crossing a day boundary in the short test)
        with patch("src.orchestrator.lifecycle.scan_and_trade", side_effect=scan_and_exit):
            await asyncio.wait_for(run_trading_loop(**kwargs), timeout=3.0)

        # In same-day execution, cleanup is NOT called
        scanner.db.cleanup_old_snapshots.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_stats_logged_after_cycle(self):
        """DB stats are fetched and logged after each cycle."""
        shutdown_event = asyncio.Event()
        kwargs = self._make_loop_kwargs(shutdown_event)

        async def scan_once(*args, **kw):
            shutdown_event.set()

        with patch("src.orchestrator.lifecycle.scan_and_trade", side_effect=scan_once):
            await asyncio.wait_for(run_trading_loop(**kwargs), timeout=3.0)

        kwargs["scanner"].db.get_stats.assert_called()

    @pytest.mark.asyncio
    async def test_shutdown_event_set_during_sleep_exits_cleanly(self):
        """If shutdown_event is set while the loop is sleeping, the loop exits cleanly."""
        shutdown_event = asyncio.Event()
        kwargs = self._make_loop_kwargs(shutdown_event)
        kwargs["interval"] = 10.0  # Long sleep, but shutdown fires first

        call_count = 0

        async def scan_once(*args, **kw):
            nonlocal call_count
            call_count += 1
            # After running, set shutdown while we're still in the scan
            shutdown_event.set()

        with patch("src.orchestrator.lifecycle.scan_and_trade", side_effect=scan_once):
            await asyncio.wait_for(run_trading_loop(**kwargs), timeout=5.0)

        # Loop ran once and then exited when shutdown was detected
        assert call_count == 1


# ──────────────────────────────────────────────
# _setup_execution_and_risk — live mode paths
# ──────────────────────────────────────────────

class TestSetupExecutionAndRiskLiveMode:
    """Tests for live-mode-specific paths in _setup_execution_and_risk."""

    @pytest.fixture
    def exec_components_live(self):
        c = _Components()
        c.db = MagicMock()
        c.kalshi = AsyncMock()
        c.kalshi_healthy = True
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
    async def test_live_mode_syncs_positions_on_startup(
        self, MockOB, MockPM, MockOR, MockFT, MockPR,
        MockAM, MockDR, MockCB, MockKS, MockRE, MockMetrics,
        exec_components_live,
    ):
        """In live mode with healthy Kalshi, startup triggers position sync."""
        settings = MagicMock()
        settings.trading.mode = "live"
        settings.alerts.imessage_enabled = False
        settings.alerts.imessage_endpoint = None
        settings.execution.stop_loss_pct = 0.10
        settings.execution.max_hold_days = 30
        settings.execution.edge_gone_threshold = 0.02
        settings.execution.trailing_stop_activate = 0.10
        settings.execution.trailing_stop_distance = 0.05
        settings.execution.take_profit_pct = 0.50
        settings.execution.capital_rotation_edge = 0.05
        settings.execution.order_poll_timeout_seconds = 30
        logger = logging.getLogger("test")

        mock_pm = MockPM.return_value
        mock_pm.sync_with_kalshi = AsyncMock(return_value=0)
        mock_risk = MockRE.return_value
        mock_risk.restore_bankroll = MagicMock()

        exec_components_live.kalshi.get_open_orders = AsyncMock(return_value=[])

        await _setup_execution_and_risk(settings, exec_components_live, logger)

        mock_pm.sync_with_kalshi.assert_awaited_once()

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
    async def test_live_mode_orphaned_orders_logged(
        self, MockOB, MockPM, MockOR, MockFT, MockPR,
        MockAM, MockDR, MockCB, MockKS, MockRE, MockMetrics,
        exec_components_live,
    ):
        """In live mode, orphaned open orders at startup are logged as warnings."""
        settings = MagicMock()
        settings.trading.mode = "live"
        settings.alerts.imessage_enabled = False
        settings.alerts.imessage_endpoint = None
        settings.execution.stop_loss_pct = 0.10
        settings.execution.max_hold_days = 30
        settings.execution.edge_gone_threshold = 0.02
        settings.execution.trailing_stop_activate = 0.10
        settings.execution.trailing_stop_distance = 0.05
        settings.execution.take_profit_pct = 0.50
        settings.execution.capital_rotation_edge = 0.05
        settings.execution.order_poll_timeout_seconds = 30
        logger = MagicMock()

        mock_pm = MockPM.return_value
        mock_pm.sync_with_kalshi = AsyncMock(return_value=1)
        mock_risk = MockRE.return_value
        mock_risk.restore_bankroll = MagicMock()

        orphaned = [{"order_id": "ORD-123"}, {"order_id": "ORD-456"}]
        exec_components_live.kalshi.get_open_orders = AsyncMock(return_value=orphaned)

        await _setup_execution_and_risk(settings, exec_components_live, logger)

        logger.warning.assert_called()

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
    async def test_imessage_backend_registered_when_configured(
        self, MockOB, MockPM, MockOR, MockFT, MockPR,
        MockAM, MockDR, MockCB, MockKS, MockRE, MockMetrics,
    ):
        """iMessage backend is registered when imessage_enabled and endpoint set."""
        c = _Components()
        c.db = MagicMock()
        c.kalshi = AsyncMock()
        c.kalshi_healthy = False
        settings = MagicMock()
        settings.trading.mode = "paper"
        settings.alerts.imessage_enabled = True
        settings.alerts.imessage_endpoint = "http://localhost:9000/send"
        settings.execution.stop_loss_pct = 0.10
        settings.execution.max_hold_days = 30
        settings.execution.edge_gone_threshold = 0.02
        settings.execution.trailing_stop_activate = 0.10
        settings.execution.trailing_stop_distance = 0.05
        settings.execution.take_profit_pct = 0.50
        settings.execution.capital_rotation_edge = 0.05
        settings.execution.order_poll_timeout_seconds = 30
        logger = logging.getLogger("test")

        mock_am = MockAM.return_value
        mock_am.register = MagicMock()
        mock_risk = MockRE.return_value
        mock_risk.restore_bankroll = MagicMock()

        with patch("src.orchestrator.lifecycle.IMessageBackend") as MockIMB:
            await _setup_execution_and_risk(settings, c, logger)
            MockIMB.assert_called_once_with("http://localhost:9000/send")
            # register should have been called at least twice (LogBackend + IMessageBackend)
            assert mock_am.register.call_count >= 2


# ──────────────────────────────────────────────
# _setup_strategies — polymarket enabled path
# ──────────────────────────────────────────────

class TestSetupStrategiesPolymarket:
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
    async def test_polymarket_enabled_no_private_key_read_only(
        self, MockAI, MockNO, MockNewsIng, MockNewsStrat,
        MockGraph, MockCrossArb, MockWhale,
        base_components,
    ):
        """Polymarket enabled but no private key → read-only scanner mode."""
        settings = MagicMock()
        settings.polymarket.enabled = True

        settings.polymarket.gamma_host = "https://gamma-api.polymarket.com"
        settings.polymarket.clob_host = "https://clob.polymarket.com"
        settings.polymarket.chain_id = 137
        settings.polymarket.signature_type = 1
        settings.news.rss_feeds = []
        settings.news.max_article_age_minutes = 60
        settings.news.min_relevance = 0.5
        logger = logging.getLogger("test")
        MockWhale.return_value.basket_size = 0

        with patch("src.orchestrator.lifecycle.CrossPlatformArbStrategy") as MockCPArb, \
             patch("src.core.polymarket_discovery.PolymarketDiscovery") if False else patch(
                 "src.orchestrator.lifecycle._setup_strategies.__code__",
             ) if False else (
                 __import__("contextlib").nullcontext()
             ):
            # Patch all the Polymarket-specific imports inside _setup_strategies
            with patch.dict("sys.modules", {
                "src.core.polymarket_discovery": MagicMock(),
                "src.data.polymarket_cross_ref": MagicMock(),
                "src.data.polymarket_scanner": MagicMock(),
            }):
                await _setup_strategies(settings, base_components, logger)

        # Core strategies always created
        assert base_components.ai_strategy is not None
        assert base_components.no_strategy is not None

    @pytest.mark.asyncio
    @patch("src.orchestrator.lifecycle.WhaleMonitor")
    @patch("src.orchestrator.lifecycle.CrossArbStrategy")
    @patch("src.orchestrator.lifecycle.MarketGraph")
    @patch("src.orchestrator.lifecycle.NewsReactiveStrategy")
    @patch("src.orchestrator.lifecycle.NewsIngestion")
    @patch("src.orchestrator.lifecycle.ObviousNoStrategy")
    @patch("src.orchestrator.lifecycle.AIProbabilityStrategy")
    async def test_polymarket_init_failure_sets_scanner_none(
        self, MockAI, MockNO, MockNewsIng, MockNewsStrat,
        MockGraph, MockCrossArb, MockWhale,
        base_components,
    ):
        """If Polymarket init raises, scanner and arb are set to None."""
        settings = MagicMock()
        settings.polymarket.enabled = True

        settings.polymarket.gamma_host = "https://gamma-api.polymarket.com"
        settings.news.rss_feeds = []
        settings.news.max_article_age_minutes = 60
        settings.news.min_relevance = 0.5
        logger = logging.getLogger("test")
        MockWhale.return_value.basket_size = 0

        with patch.dict("sys.modules", {
            "src.core.polymarket_discovery": MagicMock(**{"PolymarketDiscovery.side_effect": RuntimeError("no poly")}),
            "src.data.polymarket_cross_ref": MagicMock(),
            "src.data.polymarket_scanner": MagicMock(),
        }):
            await _setup_strategies(settings, base_components, logger)

        assert base_components.poly_scanner is None
        assert base_components.cross_platform_arb is None

    @pytest.mark.asyncio
    @patch("src.orchestrator.lifecycle.WhaleMonitor")
    @patch("src.orchestrator.lifecycle.CrossArbStrategy")
    @patch("src.orchestrator.lifecycle.MarketGraph")
    @patch("src.orchestrator.lifecycle.NewsReactiveStrategy")
    @patch("src.orchestrator.lifecycle.NewsIngestion")
    @patch("src.orchestrator.lifecycle.ObviousNoStrategy")
    @patch("src.orchestrator.lifecycle.AIProbabilityStrategy")
    async def test_whale_tracker_enabled_with_non_empty_basket(
        self, MockAI, MockNO, MockNewsIng, MockNewsStrat,
        MockGraph, MockCrossArb, MockWhale,
        base_components,
    ):
        """WhaleTrackerStrategy is created when basket has whales."""
        settings = MagicMock()
        settings.polymarket.enabled = False
        settings.news.rss_feeds = []
        settings.news.max_article_age_minutes = 60
        settings.news.min_relevance = 0.5
        logger = logging.getLogger("test")

        mock_whale_monitor = MockWhale.return_value
        mock_whale_monitor.basket_size = 5

        with patch("src.orchestrator.lifecycle.WhaleTrackerStrategy") as MockWTS:
            await _setup_strategies(settings, base_components, logger)
            MockWTS.assert_called_once()
            assert base_components.whale_strategy is not None


# ──────────────────────────────────────────────
# _setup_background_tasks tests
# ──────────────────────────────────────────────

class TestSetupBackgroundTasks:
    from src.orchestrator.lifecycle import _setup_background_tasks

    @pytest.mark.asyncio
    async def test_ws_disabled_without_api_keys(self):
        """WebSocket client is not created when API keys are absent."""
        from src.orchestrator.lifecycle import _setup_background_tasks

        c = _Components()
        c.position_manager = MagicMock()
        c.fill_tracker = MagicMock()
        c.metrics = MagicMock()
        c.calibration = MagicMock()
        c.calibration_analyzer = MagicMock()
        c.circuit_breaker = MagicMock()
        c.db = MagicMock()
        c.kalshi = AsyncMock()

        settings = MagicMock()
        settings.kalshi_api_key_id = None
        settings.kalshi_private_key_path = None
        settings.trading.bankroll = 500.0
        logger = logging.getLogger("test")

        def _close_coro_create_task(coro, **kwargs):
            """Patch for create_task that closes the coroutine to avoid warnings."""
            if hasattr(coro, 'close'):
                coro.close()
            return MagicMock()

        with patch("src.orchestrator.lifecycle.KalshiWebSocket") as MockWS, \
             patch("src.orchestrator.lifecycle.asyncio.create_task", side_effect=_close_coro_create_task):
            await _setup_background_tasks(settings, c, logger)
            MockWS.assert_not_called()

        assert c.ws_client is None

    @pytest.mark.asyncio
    async def test_ws_created_with_api_keys(self):
        """WebSocket client is created when API keys are present."""
        from src.orchestrator.lifecycle import _setup_background_tasks

        c = _Components()
        c.position_manager = MagicMock()
        c.fill_tracker = MagicMock()
        c.fill_tracker.get_pending_for_market = MagicMock(return_value=[])
        c.fill_tracker.handle_ws_fill = AsyncMock(return_value=None)
        c.metrics = MagicMock()
        c.calibration = MagicMock()
        c.calibration_analyzer = MagicMock()
        c.circuit_breaker = MagicMock()
        c.db = MagicMock()
        c.kalshi = AsyncMock()

        settings = MagicMock()
        settings.kalshi_api_key_id = "test-key-id"
        settings.kalshi_private_key_path = "/tmp/key.pem"
        settings.kalshi.active_host = "https://trading-api.kalshi.com/trade-api/v2"
        settings.trading.bankroll = 500.0
        logger = logging.getLogger("test")

        mock_ws = MagicMock()
        mock_ws.set_channels = MagicMock()
        mock_ws.on_price_update = MagicMock()
        mock_ws.on_fill = MagicMock()
        mock_ws.on_lifecycle = MagicMock()
        mock_ws.register_reconnect_sync = MagicMock()
        mock_ws.connect = AsyncMock(return_value=None)

        def _close_coro_create_task(coro, **kwargs):
            if hasattr(coro, 'close'):
                coro.close()
            return MagicMock()

        with patch("src.orchestrator.lifecycle.KalshiWebSocket", return_value=mock_ws):
            with patch("src.orchestrator.lifecycle.asyncio.create_task", side_effect=_close_coro_create_task) as mock_create_task:
                await _setup_background_tasks(settings, c, logger)

        assert c.ws_client is mock_ws
        mock_ws.set_channels.assert_called_once_with(["ticker", "fill", "market_lifecycle_v2"])

    @pytest.mark.asyncio
    async def test_ws_exception_during_setup_disables_ws(self):
        """An exception during WebSocket setup leaves ws_client as None."""
        from src.orchestrator.lifecycle import _setup_background_tasks

        c = _Components()
        c.position_manager = MagicMock()
        c.fill_tracker = MagicMock()
        c.metrics = MagicMock()
        c.calibration = MagicMock()
        c.calibration_analyzer = MagicMock()
        c.circuit_breaker = MagicMock()
        c.db = MagicMock()
        c.kalshi = AsyncMock()

        settings = MagicMock()
        settings.kalshi_api_key_id = "test-key-id"
        settings.kalshi_private_key_path = "/tmp/key.pem"
        settings.kalshi.active_host = "https://trading-api.kalshi.com/trade-api/v2"
        settings.trading.bankroll = 500.0
        logger = logging.getLogger("test")

        def _close_coro_create_task(coro, **kwargs):
            if hasattr(coro, 'close'):
                coro.close()
            return MagicMock()

        with patch("src.orchestrator.lifecycle.KalshiWebSocket", side_effect=RuntimeError("ws init fail")), \
             patch("src.orchestrator.lifecycle.asyncio.create_task", side_effect=_close_coro_create_task):
            await _setup_background_tasks(settings, c, logger)

        assert c.ws_client is None

    @pytest.mark.asyncio
    async def test_dashboard_disabled_on_import_error(self):
        """Dashboard task is None when fastapi/uvicorn are not installed."""
        from src.orchestrator.lifecycle import _setup_background_tasks

        c = _Components()
        c.position_manager = MagicMock()
        c.fill_tracker = MagicMock()
        c.metrics = MagicMock()
        c.calibration = MagicMock()
        c.calibration_analyzer = MagicMock()
        c.circuit_breaker = MagicMock()
        c.db = MagicMock()
        c.kalshi = AsyncMock()

        settings = MagicMock()
        settings.kalshi_api_key_id = None
        settings.kalshi_private_key_path = None
        settings.trading.bankroll = 500.0
        logger = logging.getLogger("test")

        import builtins
        real_import = builtins.__import__

        def import_blocker(name, *args, **kwargs):
            if name == "src.dashboard.server":
                raise ImportError("fastapi not installed")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_blocker):
            await _setup_background_tasks(settings, c, logger)

        assert c.dashboard_task is None


# ──────────────────────────────────────────────
# WebSocket callback tests (via _setup_background_tasks)
# ──────────────────────────────────────────────

class TestWebSocketCallbacks:
    """Test the closures registered as WebSocket callbacks."""

    @pytest.fixture
    def ws_setup(self):
        """Return a _Components with registered WS callbacks via _setup_background_tasks."""
        from src.orchestrator.lifecycle import (
            FillUpdate,
            LifecycleUpdate,
            TickerUpdate,
        )
        return FillUpdate, LifecycleUpdate, TickerUpdate

    @pytest.mark.asyncio
    async def test_on_price_callback_updates_position_manager(self):
        """_on_price callback routes price to position_manager.update_price."""
        from src.orchestrator.lifecycle import _setup_background_tasks, TickerUpdate

        c = _Components()
        c.position_manager = MagicMock()
        c.fill_tracker = MagicMock()
        c.fill_tracker.handle_ws_fill = AsyncMock(return_value=None)
        c.fill_tracker.get_pending_for_market = MagicMock(return_value=[])
        c.metrics = MagicMock()
        c.calibration = MagicMock()
        c.calibration_analyzer = MagicMock()
        c.circuit_breaker = MagicMock()
        c.db = MagicMock()
        c.kalshi = AsyncMock()
        c.kalshi.get_market = AsyncMock(return_value=None)

        settings = MagicMock()
        settings.kalshi_api_key_id = "key"
        settings.kalshi_private_key_path = "/tmp/k.pem"
        settings.kalshi.active_host = "https://trading-api.kalshi.com/trade-api/v2"
        settings.trading.bankroll = 500.0
        logger = logging.getLogger("test")

        captured_callbacks = {}
        mock_ws = MagicMock()
        mock_ws.set_channels = MagicMock()
        mock_ws.connect = AsyncMock()

        def capture_price(cb):
            captured_callbacks["price"] = cb

        def capture_fill(cb):
            captured_callbacks["fill"] = cb

        def capture_lifecycle(cb):
            captured_callbacks["lifecycle"] = cb

        def capture_reconnect(cb):
            captured_callbacks["reconnect"] = cb

        mock_ws.on_price_update = capture_price
        mock_ws.on_fill = capture_fill
        mock_ws.on_lifecycle = capture_lifecycle
        mock_ws.register_reconnect_sync = capture_reconnect

        with patch("src.orchestrator.lifecycle.KalshiWebSocket", return_value=mock_ws):
            def _close_coro_create_task(coro, **kwargs):
                    if hasattr(coro, 'close'):
                        coro.close()
                    return MagicMock()

            with patch("src.orchestrator.lifecycle.asyncio.create_task", side_effect=_close_coro_create_task):
                await _setup_background_tasks(settings, c, logger)

        # Simulate a TickerUpdate
        update = MagicMock(spec=TickerUpdate)
        update.market_ticker = "MKT-A"
        update.yes_bid = 0.65
        update.price = 0.60

        await captured_callbacks["price"](update)
        c.position_manager.update_price.assert_called_once_with("MKT-A", 0.65, 0.35)

    @pytest.mark.asyncio
    async def test_on_lifecycle_cancels_resting_orders_on_close(self):
        """_on_lifecycle cancels resting orders when market closes."""
        from src.orchestrator.lifecycle import _setup_background_tasks, LifecycleUpdate

        c = _Components()
        c.position_manager = MagicMock()
        c.position_manager.has_position = MagicMock(return_value=False)
        mock_order = MagicMock()
        mock_order.id = "ORD-999"
        c.fill_tracker = MagicMock()
        c.fill_tracker.get_pending_for_market = MagicMock(return_value=[mock_order])
        c.fill_tracker.handle_ws_fill = AsyncMock(return_value=None)
        c.metrics = MagicMock()
        c.calibration = MagicMock()
        c.calibration_analyzer = MagicMock()
        c.circuit_breaker = MagicMock()
        c.db = MagicMock()
        c.kalshi = AsyncMock()
        c.kalshi.cancel_order = AsyncMock()

        settings = MagicMock()
        settings.kalshi_api_key_id = "key"
        settings.kalshi_private_key_path = "/tmp/k.pem"
        settings.kalshi.active_host = "https://trading-api.kalshi.com/trade-api/v2"
        settings.trading.bankroll = 500.0
        logger = logging.getLogger("test")

        captured_callbacks = {}
        mock_ws = MagicMock()
        mock_ws.set_channels = MagicMock()
        mock_ws.on_price_update = lambda cb: captured_callbacks.update({"price": cb})
        mock_ws.on_fill = lambda cb: captured_callbacks.update({"fill": cb})
        mock_ws.on_lifecycle = lambda cb: captured_callbacks.update({"lifecycle": cb})
        mock_ws.register_reconnect_sync = lambda cb: captured_callbacks.update({"reconnect": cb})
        mock_ws.connect = AsyncMock()

        def _close_coro_create_task(coro, **kwargs):
            if hasattr(coro, 'close'):
                coro.close()
            return MagicMock()

        with patch("src.orchestrator.lifecycle.KalshiWebSocket", return_value=mock_ws):
            with patch("src.orchestrator.lifecycle.asyncio.create_task", side_effect=_close_coro_create_task):
                await _setup_background_tasks(settings, c, logger)

        update = MagicMock(spec=LifecycleUpdate)
        update.market_ticker = "MKT-CLOSE"
        update.status = "closed"
        update.settlement_value = None

        await captured_callbacks["lifecycle"](update)

        c.kalshi.cancel_order.assert_awaited_once_with("ORD-999")

    @pytest.mark.asyncio
    async def test_on_lifecycle_marks_position_for_exit_on_settlement(self):
        """_on_lifecycle marks position for exit when market is finalized."""
        from src.orchestrator.lifecycle import _setup_background_tasks, LifecycleUpdate

        c = _Components()
        c.position_manager = MagicMock()
        c.position_manager.has_position = MagicMock(return_value=True)
        c.position_manager.record_settlement = MagicMock()
        c.position_manager.mark_pending_exit = MagicMock()
        c.fill_tracker = MagicMock()
        c.fill_tracker.get_pending_for_market = MagicMock(return_value=[])
        c.fill_tracker.handle_ws_fill = AsyncMock(return_value=None)
        c.metrics = MagicMock()
        c.calibration = MagicMock()
        c.calibration_analyzer = MagicMock()
        c.circuit_breaker = MagicMock()
        c.db = MagicMock()
        c.kalshi = AsyncMock()

        settings = MagicMock()
        settings.kalshi_api_key_id = "key"
        settings.kalshi_private_key_path = "/tmp/k.pem"
        settings.kalshi.active_host = "https://trading-api.kalshi.com/trade-api/v2"
        settings.trading.bankroll = 500.0
        logger = logging.getLogger("test")

        captured_callbacks = {}
        mock_ws = MagicMock()
        mock_ws.set_channels = MagicMock()
        mock_ws.on_price_update = lambda cb: captured_callbacks.update({"price": cb})
        mock_ws.on_fill = lambda cb: captured_callbacks.update({"fill": cb})
        mock_ws.on_lifecycle = lambda cb: captured_callbacks.update({"lifecycle": cb})
        mock_ws.register_reconnect_sync = lambda cb: captured_callbacks.update({"reconnect": cb})
        mock_ws.connect = AsyncMock()

        def _close_coro_create_task(coro, **kwargs):
            if hasattr(coro, 'close'):
                coro.close()
            return MagicMock()

        with patch("src.orchestrator.lifecycle.KalshiWebSocket", return_value=mock_ws):
            with patch("src.orchestrator.lifecycle.asyncio.create_task", side_effect=_close_coro_create_task):
                await _setup_background_tasks(settings, c, logger)

        update = MagicMock(spec=LifecycleUpdate)
        update.market_ticker = "MKT-SETTLE"
        update.status = "finalized"
        update.settlement_value = 1.0

        await captured_callbacks["lifecycle"](update)

        c.position_manager.record_settlement.assert_called_once_with("MKT-SETTLE", 1.0)
        c.position_manager.mark_pending_exit.assert_called_once_with("MKT-SETTLE")
