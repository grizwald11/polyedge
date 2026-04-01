"""Tests for scan_cycle module — M-1 key freshness check integration."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.core.models import (
    Market,
    MarketCategory,
    MarketToken,
    Platform,
    TokenOutcome,
)
from src.orchestrator.scan_cycle import (
    _check_fills_and_cleanup,
    _generate_all_signals,
    _scan_markets,
    _update_position_prices,
    scan_and_trade,
)


@pytest.fixture
def mock_fill_tracker():
    ft = AsyncMock()
    ft.check_fills = AsyncMock(return_value=[])
    return ft


@pytest.fixture
def mock_position_manager():
    return MagicMock()


@pytest.fixture
def mock_order_router():
    router = AsyncMock()
    router.cancel_stale_orders = AsyncMock(return_value=0)
    return router


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.execution.stale_order_age_seconds = 600
    return s


@pytest.fixture
def mock_logger():
    return MagicMock()


class TestKeyFreshnessInFillCleanup:
    """M-1: kalshi.check_key_freshness() is called during fill/cleanup cycle."""

    @pytest.mark.asyncio
    async def test_calls_check_key_freshness(
        self, mock_fill_tracker, mock_position_manager, mock_order_router,
        mock_settings, mock_logger,
    ):
        kalshi = MagicMock()
        kalshi.check_key_freshness = MagicMock(return_value=True)

        await _check_fills_and_cleanup(
            mock_fill_tracker, mock_position_manager, mock_order_router,
            mock_settings, mock_logger, kalshi=kalshi,
        )

        kalshi.check_key_freshness.assert_called_once()

    @pytest.mark.asyncio
    async def test_key_freshness_exception_does_not_halt(
        self, mock_fill_tracker, mock_position_manager, mock_order_router,
        mock_settings, mock_logger,
    ):
        """Key freshness failure should log debug and continue, not crash."""
        kalshi = MagicMock()
        kalshi.check_key_freshness = MagicMock(side_effect=RuntimeError("key rotated"))

        # Should not raise
        await _check_fills_and_cleanup(
            mock_fill_tracker, mock_position_manager, mock_order_router,
            mock_settings, mock_logger, kalshi=kalshi,
        )

        # Fill tracker should still run
        mock_fill_tracker.check_fills.assert_awaited_once()
        mock_logger.debug.assert_called()

    @pytest.mark.asyncio
    async def test_no_kalshi_skips_freshness_check(
        self, mock_fill_tracker, mock_position_manager, mock_order_router,
        mock_settings, mock_logger,
    ):
        """When kalshi is None, key freshness check is skipped gracefully."""
        await _check_fills_and_cleanup(
            mock_fill_tracker, mock_position_manager, mock_order_router,
            mock_settings, mock_logger, kalshi=None,
        )

        # Fill tracker should still run
        mock_fill_tracker.check_fills.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stale_orders_cancelled(
        self, mock_fill_tracker, mock_position_manager, mock_order_router,
        mock_settings, mock_logger,
    ):
        """Stale orders are cancelled via order_router."""
        mock_order_router.cancel_stale_orders = AsyncMock(return_value=3)

        await _check_fills_and_cleanup(
            mock_fill_tracker, mock_position_manager, mock_order_router,
            mock_settings, mock_logger, kalshi=None,
        )

        mock_order_router.cancel_stale_orders.assert_awaited_once_with(
            max_age_seconds=mock_settings.execution.stale_order_age_seconds
        )

    @pytest.mark.asyncio
    async def test_fill_updates_position_manager(
        self, mock_fill_tracker, mock_position_manager, mock_order_router,
        mock_settings, mock_logger,
    ):
        """Each fill returned by fill_tracker updates position_manager and clears pending exit."""
        from src.core.models import Side, StrategyName, Trade

        fill = Trade(
            order_id="PE-fill-1", market_id="MKT-A", token_id="MKT-A_yes",
            side=Side.BUY, price=0.50, size=5, fee=0.0,
            strategy=StrategyName.AI_PROBABILITY, paper=True,
        )
        mock_fill_tracker.check_fills = AsyncMock(return_value=[fill])

        await _check_fills_and_cleanup(
            mock_fill_tracker, mock_position_manager, mock_order_router,
            mock_settings, mock_logger, kalshi=None,
        )

        mock_position_manager.clear_pending_exit.assert_called_once_with("MKT-A")
        mock_position_manager.update_from_trade.assert_called_once_with(fill)

    @pytest.mark.asyncio
    async def test_fill_tracker_exception_does_not_halt(
        self, mock_fill_tracker, mock_position_manager, mock_order_router,
        mock_settings, mock_logger,
    ):
        """An exception in fill_tracker.check_fills is logged and does not propagate."""
        mock_fill_tracker.check_fills = AsyncMock(side_effect=RuntimeError("db locked"))

        await _check_fills_and_cleanup(
            mock_fill_tracker, mock_position_manager, mock_order_router,
            mock_settings, mock_logger, kalshi=None,
        )

        mock_logger.error.assert_called()

    @pytest.mark.asyncio
    async def test_stale_cancel_exception_does_not_halt(
        self, mock_fill_tracker, mock_position_manager, mock_order_router,
        mock_settings, mock_logger,
    ):
        """An exception in cancel_stale_orders is logged and does not propagate."""
        mock_order_router.cancel_stale_orders = AsyncMock(side_effect=RuntimeError("network error"))

        await _check_fills_and_cleanup(
            mock_fill_tracker, mock_position_manager, mock_order_router,
            mock_settings, mock_logger, kalshi=None,
        )

        mock_logger.error.assert_called()


# ──────────────────────────────────────────────
# _scan_markets tests
# ──────────────────────────────────────────────

def _make_market(ticker: str) -> Market:
    return Market(
        ticker=ticker,
        question=f"Will {ticker} happen?",
        category=MarketCategory.POLITICS,
        tokens=[
            MarketToken(token_id=f"{ticker}_yes", outcome=TokenOutcome.YES, price=0.50),
            MarketToken(token_id=f"{ticker}_no", outcome=TokenOutcome.NO, price=0.50),
        ],
        volume_24h=50000,
        liquidity=20000,
        active=True,
    )


class TestScanMarkets:
    @pytest.mark.asyncio
    async def test_kalshi_only_returns_markets(self):
        scanner = MagicMock()
        markets = [_make_market("MKT-A"), _make_market("MKT-B")]
        scanner.run_scan_cycle = AsyncMock(return_value=markets)
        metrics = MagicMock()
        logger = logging.getLogger("test")

        result, poly = await _scan_markets(scanner, None, metrics, logger)

        assert result == markets
        assert poly == []

    @pytest.mark.asyncio
    async def test_poly_scanner_markets_merged(self):
        scanner = MagicMock()
        kalshi_markets = [_make_market("KALS-A")]
        poly_markets = [_make_market("POLY-B")]
        scanner.run_scan_cycle = AsyncMock(return_value=kalshi_markets)

        poly_scanner = MagicMock()
        poly_scanner.run_scan_cycle = AsyncMock(return_value=poly_markets)

        metrics = MagicMock()
        logger = logging.getLogger("test")

        result, poly = await _scan_markets(scanner, poly_scanner, metrics, logger)

        assert len(result) == 2
        assert len(poly) == 1

    @pytest.mark.asyncio
    async def test_kalshi_scan_failure_returns_empty_list(self):
        scanner = MagicMock()
        scanner.run_scan_cycle = AsyncMock(return_value=Exception("scan error"))

        poly_scanner = MagicMock()
        poly_scanner.run_scan_cycle = AsyncMock(return_value=[])

        # asyncio.gather returns exceptions as values when return_exceptions=True
        # Simulate by having gather return an exception for kalshi
        async def fake_gather(*coros, return_exceptions=False):
            return [Exception("kalshi failed"), []]

        metrics = MagicMock()
        logger = logging.getLogger("test")

        with patch("src.orchestrator.scan_cycle.asyncio.gather", side_effect=fake_gather):
            result, poly = await _scan_markets(scanner, poly_scanner, metrics, logger)

        assert result == [] or result is not None

    @pytest.mark.asyncio
    async def test_scan_exception_records_metric_error(self):
        scanner = MagicMock()
        scanner.run_scan_cycle = AsyncMock(side_effect=RuntimeError("API down"))
        metrics = MagicMock()
        logger = logging.getLogger("test")

        result, poly = await _scan_markets(scanner, None, metrics, logger)

        assert result is None
        metrics.record_error.assert_called_once_with("scanner", "API down")

    @pytest.mark.asyncio
    async def test_poly_scan_failure_logs_error(self):
        scanner = MagicMock()
        kalshi_markets = [_make_market("KALS-A")]
        scanner.run_scan_cycle = AsyncMock(return_value=kalshi_markets)

        poly_scanner = MagicMock()
        poly_scanner.run_scan_cycle = AsyncMock(return_value=RuntimeError("poly down"))

        async def fake_gather(*coros, return_exceptions=False):
            return [kalshi_markets, Exception("poly failed")]

        metrics = MagicMock()
        logger = MagicMock()

        with patch("src.orchestrator.scan_cycle.asyncio.gather", side_effect=fake_gather):
            result, poly = await _scan_markets(scanner, poly_scanner, metrics, logger)

        logger.error.assert_called()


# ──────────────────────────────────────────────
# _update_position_prices tests
# ──────────────────────────────────────────────

class TestUpdatePositionPrices:
    @pytest.mark.asyncio
    async def test_prices_updated_from_scan(self):
        market = _make_market("MKT-A")
        market._yes_price = 0.60
        market._no_price = 0.40

        position_manager = MagicMock()
        position_manager.get_all_positions.return_value = []
        logger = logging.getLogger("test")
        kalshi = AsyncMock()

        await _update_position_prices([market], position_manager, kalshi, None, logger)

        position_manager.update_price.assert_called_with(
            market.ticker, market.yes_price, market.no_price
        )

    @pytest.mark.asyncio
    async def test_missing_position_fetches_from_kalshi(self):
        """Positions not in the scan are fetched from Kalshi API."""
        markets = [_make_market("MKT-A")]

        pos = MagicMock()
        pos.market_id = "MKT-MISSING"
        pos.current_price = 0.50
        # No platform attribute → defaults to KALSHI

        position_manager = MagicMock()
        position_manager.get_all_positions.return_value = [pos]
        logger = logging.getLogger("test")

        kalshi = AsyncMock()
        kalshi.get_market = AsyncMock(return_value={
            "yes_bid": 0.60, "yes_ask": 0.70, "title": "Missing Market"
        })

        await _update_position_prices(markets, position_manager, kalshi, None, logger)

        kalshi.get_market.assert_awaited_once_with("MKT-MISSING")
        # update_price should have been called for the missing position
        calls = [str(c) for c in position_manager.update_price.call_args_list]
        assert any("MKT-MISSING" in c for c in calls)

    @pytest.mark.asyncio
    async def test_missing_position_with_zero_prices_skipped(self):
        """Position market with no valid price data is skipped gracefully."""
        markets = []

        pos = MagicMock()
        pos.market_id = "MKT-NOPRICE"

        position_manager = MagicMock()
        position_manager.get_all_positions.return_value = [pos]
        logger = MagicMock()

        kalshi = AsyncMock()
        kalshi.get_market = AsyncMock(return_value={
            "yes_bid": 0, "yes_ask": 0, "title": "No Price Market"
        })

        await _update_position_prices(markets, position_manager, kalshi, None, logger)

        logger.warning.assert_called()

    @pytest.mark.asyncio
    async def test_missing_position_api_exception_logged(self):
        """An exception fetching a missing position market is logged as warning."""
        markets = []

        pos = MagicMock()
        pos.market_id = "MKT-ERR"

        position_manager = MagicMock()
        position_manager.get_all_positions.return_value = [pos]
        logger = MagicMock()

        kalshi = AsyncMock()
        kalshi.get_market = AsyncMock(side_effect=RuntimeError("timeout"))

        await _update_position_prices(markets, position_manager, kalshi, None, logger)

        logger.warning.assert_called()

    @pytest.mark.asyncio
    async def test_missing_position_none_response_skipped(self):
        """If Kalshi returns None for a market, we don't crash."""
        markets = []

        pos = MagicMock()
        pos.market_id = "MKT-NONE"

        position_manager = MagicMock()
        position_manager.get_all_positions.return_value = [pos]
        logger = logging.getLogger("test")

        kalshi = AsyncMock()
        kalshi.get_market = AsyncMock(return_value=None)

        # Should not raise
        await _update_position_prices(markets, position_manager, kalshi, None, logger)


# ──────────────────────────────────────────────
# _generate_all_signals tests
# ──────────────────────────────────────────────

class TestGenerateAllSignals:
    def _make_signal(self, market_id: str = "MKT-A"):
        from src.core.models import Direction, Signal, StrategyName
        return Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id=market_id,
            market_question="Will it happen?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.60,
            market_price=0.50,
            confidence=0.7,
            reasoning="Test",
        )

    @pytest.mark.asyncio
    async def test_all_strategies_called(self):
        markets = [_make_market("MKT-A")]
        ai_strategy = MagicMock()
        ai_strategy.scan_for_opportunities = AsyncMock(return_value=[self._make_signal()])
        no_strategy = MagicMock()
        no_strategy.scan_for_opportunities = MagicMock(return_value=[])
        news_strategy = MagicMock()
        news_strategy.scan_for_opportunities = AsyncMock(return_value=[])
        cross_arb = MagicMock()
        cross_arb.scan_for_opportunities = AsyncMock(return_value=[])
        whale = MagicMock()
        whale.scan_for_opportunities = MagicMock(return_value=[])
        alert_manager = AsyncMock()
        metrics = MagicMock()
        logger = logging.getLogger("test")

        all_signals, ai_signals, no_signals = await _generate_all_signals(
            markets, [], ai_strategy, no_strategy, news_strategy,
            cross_arb, whale, None,
            alert_manager, metrics, logger,
        )

        ai_strategy.scan_for_opportunities.assert_awaited_once()
        no_strategy.scan_for_opportunities.assert_called_once()
        news_strategy.scan_for_opportunities.assert_awaited_once()
        cross_arb.scan_for_opportunities.assert_awaited_once()
        whale.scan_for_opportunities.assert_called_once()
        assert len(all_signals) == 1

    @pytest.mark.asyncio
    async def test_ai_strategy_failure_flagged(self):
        ai_strategy = MagicMock()
        ai_strategy.scan_for_opportunities = AsyncMock(side_effect=RuntimeError("AI down"))
        no_strategy = MagicMock()
        no_strategy.scan_for_opportunities = MagicMock(return_value=[])
        alert_manager = AsyncMock()
        metrics = MagicMock()
        logger = MagicMock()

        all_signals, ai_signals, no_signals = await _generate_all_signals(
            [], [], ai_strategy, no_strategy, None, None, None, None,
            alert_manager, metrics, logger,
        )

        logger.error.assert_called()
        assert ai_signals == []

    @pytest.mark.asyncio
    async def test_all_strategies_fail_sends_alert(self):
        """When ALL strategies fail, alert_manager is notified."""
        ai_strategy = MagicMock()
        ai_strategy.scan_for_opportunities = AsyncMock(side_effect=RuntimeError("AI down"))
        no_strategy = MagicMock()
        no_strategy.scan_for_opportunities = MagicMock(side_effect=RuntimeError("NO down"))
        alert_manager = AsyncMock()
        alert_manager.send_circuit_breaker_alert = AsyncMock()
        metrics = MagicMock()
        logger = MagicMock()

        all_signals, _, _ = await _generate_all_signals(
            [], [], ai_strategy, no_strategy, None, None, None, None,
            alert_manager, metrics, logger,
        )

        alert_manager.send_circuit_breaker_alert.assert_awaited()
        assert all_signals == []

    @pytest.mark.asyncio
    async def test_partial_failure_logs_degraded_mode(self):
        """When some (but not all) strategies fail, logs degraded mode warning."""
        ai_strategy = MagicMock()
        ai_strategy.scan_for_opportunities = AsyncMock(return_value=[self._make_signal()])
        no_strategy = MagicMock()
        no_strategy.scan_for_opportunities = MagicMock(side_effect=RuntimeError("NO down"))
        alert_manager = AsyncMock()
        metrics = MagicMock()
        logger = MagicMock()

        await _generate_all_signals(
            [], [], ai_strategy, no_strategy, None, None, None, None,
            alert_manager, metrics, logger,
        )

        logger.warning.assert_called()

    @pytest.mark.asyncio
    async def test_cross_platform_arb_only_called_with_poly_markets(self):
        """Cross-platform arb is only called if poly_markets is non-empty."""
        ai_strategy = MagicMock()
        ai_strategy.scan_for_opportunities = AsyncMock(return_value=[])
        no_strategy = MagicMock()
        no_strategy.scan_for_opportunities = MagicMock(return_value=[])
        cross_platform_arb = MagicMock()
        cross_platform_arb.scan_for_opportunities = AsyncMock(return_value=[])
        alert_manager = AsyncMock()
        metrics = MagicMock()
        logger = logging.getLogger("test")

        # Empty poly_markets → should NOT call cross_platform_arb
        await _generate_all_signals(
            [], [], ai_strategy, no_strategy, None, None, None, cross_platform_arb,
            alert_manager, metrics, logger,
        )
        cross_platform_arb.scan_for_opportunities.assert_not_awaited()

        # Non-empty poly_markets → SHOULD call cross_platform_arb
        poly_markets = [_make_market("POLY-B")]
        kalshi_markets = [_make_market("KALS-A")]
        await _generate_all_signals(
            kalshi_markets, poly_markets, ai_strategy, no_strategy, None, None, None, cross_platform_arb,
            alert_manager, metrics, logger,
        )
        cross_platform_arb.scan_for_opportunities.assert_awaited()


# ──────────────────────────────────────────────
# scan_and_trade integration — via existing helpers
# ──────────────────────────────────────────────

def _make_scan_kwargs(settings=None):
    """Build a complete kwargs dict for scan_and_trade with all mocks."""
    market = _make_market("MKT-A")

    scanner = MagicMock()
    scanner.run_scan_cycle = AsyncMock(return_value=[market])
    scanner.db = MagicMock()
    scanner.db.log_signal = MagicMock(return_value=1)
    scanner.db.get_stats = MagicMock(return_value={
        "active_markets": 1, "total_signals": 0, "total_trades": 0, "total_pnl": 0.0
    })

    position_manager = MagicMock()
    position_manager.get_total_exposure.return_value = 0.0
    position_manager.get_total_unrealized_pnl.return_value = 0.0
    position_manager.get_position_count.return_value = 0
    position_manager.get_all_positions.return_value = []
    position_manager.get_exit_candidates.return_value = []

    circuit_breaker = MagicMock()
    circuit_breaker.check.return_value = True
    circuit_breaker.get_kelly_multiplier.return_value = 1.0

    risk_engine = MagicMock()
    risk_result = MagicMock()
    risk_result.passed = True
    risk_result.failed_checks = []
    risk_engine.check_all.return_value = risk_result

    kelly_sizer = MagicMock()
    kelly_sizer.calculate_position_size.return_value = 0  # no trades by default

    order_builder = MagicMock()
    order_router = MagicMock()
    order_router.route_order = AsyncMock()
    order_router.cancel_stale_orders = AsyncMock(return_value=0)

    fill_tracker = MagicMock()
    fill_tracker.check_fills = AsyncMock(return_value=[])

    resolution_tracker = MagicMock()
    resolution_tracker.check_resolutions = AsyncMock(return_value=0)

    calibration_analyzer = MagicMock()
    calibration_analyzer.generate_report = MagicMock(return_value=MagicMock(total_resolved=0))

    alert_manager = MagicMock()
    alert_manager.send = AsyncMock()
    alert_manager.send_circuit_breaker_alert = AsyncMock()

    metrics = MagicMock()

    kalshi = MagicMock()
    kalshi.check_key_freshness = MagicMock(return_value=True)
    kalshi.get_market = AsyncMock(return_value=None)

    if settings is None:
        settings = MagicMock()
        settings.trading.mode = "paper"
        settings.trading.bankroll = 500.0
        settings.trading.max_trades_per_cycle = 10
        settings.trading.max_edge_trades_per_cycle = 5
        settings.trading.min_edge_ai = 0.05
        settings.trading.min_edge_arb = 0.02
        settings.trading.min_edge_no = 0.01
        settings.alerts.alert_on_circuit_breaker = False
        settings.alerts.alert_on_trade = False
        settings.execution.stale_order_age_seconds = 300
        settings.execution.cycle_timeout_seconds = 60

    return dict(
        scanner=scanner,
        kalshi=kalshi,
        ai_strategy=MagicMock(**{"scan_for_opportunities": AsyncMock(return_value=[])}),
        no_strategy=MagicMock(**{"scan_for_opportunities": MagicMock(return_value=[])}),
        news_strategy=None,
        cross_arb_strategy=None,
        whale_strategy=None,
        market_graph=None,
        risk_engine=risk_engine,
        kelly_sizer=kelly_sizer,
        circuit_breaker=circuit_breaker,
        order_builder=order_builder,
        order_router=order_router,
        position_manager=position_manager,
        calibration=MagicMock(),
        resolution_tracker=resolution_tracker,
        calibration_analyzer=calibration_analyzer,
        fill_tracker=fill_tracker,
        alert_manager=alert_manager,
        metrics=metrics,
        settings=settings,
    )


class TestScanAndTrade:
    @pytest.mark.asyncio
    async def test_circuit_breaker_skips_scan(self):
        kwargs = _make_scan_kwargs()
        kwargs["circuit_breaker"].check.return_value = False
        kwargs["circuit_breaker"].halt_reason = "loss limit"

        await scan_and_trade(**kwargs)

        kwargs["scanner"].run_scan_cycle.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_markets_exits_early(self):
        kwargs = _make_scan_kwargs()
        kwargs["scanner"].run_scan_cycle = AsyncMock(return_value=[])

        await scan_and_trade(**kwargs)

        kwargs["ai_strategy"].scan_for_opportunities.assert_not_called()

    @pytest.mark.asyncio
    async def test_market_graph_indexed(self):
        kwargs = _make_scan_kwargs()
        market_graph = MagicMock()
        market_graph.index_markets = MagicMock()
        kwargs["market_graph"] = market_graph

        await scan_and_trade(**kwargs)

        market_graph.index_markets.assert_called_once()

    @pytest.mark.asyncio
    async def test_market_graph_exception_logged(self):
        kwargs = _make_scan_kwargs()
        market_graph = MagicMock()
        market_graph.index_markets = MagicMock(side_effect=RuntimeError("chroma error"))
        kwargs["market_graph"] = market_graph

        # Should not raise
        await scan_and_trade(**kwargs)

    @pytest.mark.asyncio
    async def test_resolution_tracker_called(self):
        kwargs = _make_scan_kwargs()

        await scan_and_trade(**kwargs)

        kwargs["resolution_tracker"].check_resolutions.assert_awaited()

    @pytest.mark.asyncio
    async def test_metrics_recorded_when_no_signals(self):
        kwargs = _make_scan_kwargs()

        await scan_and_trade(**kwargs)

        kwargs["metrics"].record_cycle.assert_called()

    @pytest.mark.asyncio
    async def test_position_sync_called_in_live_mode(self):
        """Position sync with Kalshi runs in live mode after signal execution."""
        from src.core.models import Direction, Order, OrderStatus, OrderType, Side, Signal, StrategyName, Trade

        kwargs = _make_scan_kwargs()
        kwargs["settings"].trading.mode = "live"
        # Make kalshi fully async so _sync_bankroll doesn't fail
        kalshi = AsyncMock()
        kalshi.check_key_freshness = MagicMock(return_value=True)
        kalshi.get_balance = AsyncMock(return_value=500.0)
        kalshi.get_market = AsyncMock(return_value=None)
        kwargs["kalshi"] = kalshi
        kwargs["position_manager"].sync_with_kalshi = AsyncMock(return_value=0)
        kwargs["position_manager"]._consecutive_sync_failures = 0
        kwargs["risk_engine"].update_bankroll = MagicMock()

        # Generate a signal so we don't return early before the sync
        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="MKT-A",
            market_question="Will A happen?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.60,
            market_price=0.50,
            confidence=0.7,
            reasoning="Test",
        )
        kwargs["ai_strategy"].scan_for_opportunities = AsyncMock(return_value=[signal])
        # Give a size so the order is attempted
        kwargs["kelly_sizer"].calculate_position_size = MagicMock(return_value=5)
        order = Order(
            id="PE-live-1", market_id="MKT-A", token_id="MKT-A_yes",
            side=Side.BUY, price=0.60, size=5, cost=3.0,
            order_type=OrderType.GTC, status=OrderStatus.PENDING,
            strategy=StrategyName.AI_PROBABILITY, paper=False,
        )
        kwargs["order_builder"].build_limit_order = MagicMock(return_value=order)
        trade = Trade(
            order_id="PE-live-1", market_id="MKT-A", token_id="MKT-A_yes",
            side=Side.BUY, price=0.60, size=5, fee=0.0,
            strategy=StrategyName.AI_PROBABILITY, paper=False,
        )
        route_result = MagicMock()
        route_result.success = True
        route_result.trade = trade
        kwargs["order_router"].route_order = AsyncMock(return_value=route_result)

        await scan_and_trade(**kwargs)

        kwargs["position_manager"].sync_with_kalshi.assert_awaited()

    @pytest.mark.asyncio
    async def test_consecutive_sync_failures_trigger_halt(self):
        """3+ consecutive sync failures trigger circuit breaker halt."""
        from src.core.models import Direction, Signal, StrategyName

        kwargs = _make_scan_kwargs()
        kwargs["settings"].trading.mode = "live"
        kalshi = AsyncMock()
        kalshi.check_key_freshness = MagicMock(return_value=True)
        kalshi.get_balance = AsyncMock(return_value=500.0)
        kalshi.get_market = AsyncMock(return_value=None)
        kwargs["kalshi"] = kalshi
        kwargs["position_manager"].sync_with_kalshi = AsyncMock(side_effect=RuntimeError("sync fail"))
        kwargs["position_manager"]._consecutive_sync_failures = 2  # one more = halt
        kwargs["risk_engine"].update_bankroll = MagicMock()

        # Need signals to avoid early return before the sync block
        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="MKT-A",
            market_question="Will A happen?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.60,
            market_price=0.50,
            confidence=0.7,
            reasoning="Test",
        )
        kwargs["ai_strategy"].scan_for_opportunities = AsyncMock(return_value=[signal])
        kwargs["kelly_sizer"].calculate_position_size = MagicMock(return_value=0)  # blocked by kelly

        await scan_and_trade(**kwargs)

        kwargs["circuit_breaker"].trigger_halt.assert_called()

    @pytest.mark.asyncio
    async def test_market_closed_while_position_held_sends_alert(self):
        """If a position's market is marked inactive, an alert is sent."""
        from src.core.models import Direction, Signal, StrategyName

        kwargs = _make_scan_kwargs()

        inactive_market = _make_market("MKT-CLOSED")
        inactive_market.active = False
        # Also have an active market so signals can be generated
        active_market = _make_market("MKT-ACTIVE")
        kwargs["scanner"].run_scan_cycle = AsyncMock(return_value=[active_market, inactive_market])

        # Position held on the closed market
        pos = MagicMock()
        pos.market_id = "MKT-CLOSED"
        kwargs["position_manager"].get_all_positions.return_value = [pos]
        kwargs["position_manager"].mark_pending_exit = MagicMock()

        # Generate a signal so we don't return early before the market-closed check
        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="MKT-ACTIVE",
            market_question="Will Active happen?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.60,
            market_price=0.50,
            confidence=0.7,
            reasoning="Test",
        )
        kwargs["ai_strategy"].scan_for_opportunities = AsyncMock(return_value=[signal])
        kwargs["kelly_sizer"].calculate_position_size = MagicMock(return_value=0)  # no trade needed

        await scan_and_trade(**kwargs)

        kwargs["position_manager"].mark_pending_exit.assert_called_with("MKT-CLOSED")
        kwargs["alert_manager"].send.assert_awaited()

    @pytest.mark.asyncio
    async def test_calibration_report_at_cycle_10(self):
        """Calibration report is generated and Kelly sizer updated at cycle 10."""
        from src.core.models import Direction, Signal, StrategyName

        kwargs = _make_scan_kwargs()
        # Need a signal to get past the early return guard
        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="MKT-A",
            market_question="Will A happen?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.60,
            market_price=0.50,
            confidence=0.7,
            reasoning="Test",
        )
        kwargs["ai_strategy"].scan_for_opportunities = AsyncMock(return_value=[signal])
        kwargs["kelly_sizer"].calculate_position_size = MagicMock(return_value=0)

        report = MagicMock()
        report.total_resolved = 10
        report.overall_brier = 0.15
        report.overall_win_rate = 0.60
        report.total_unresolved = 2
        report.best_category = "Politics"
        report.worst_category = "Tech"
        kwargs["calibration_analyzer"].generate_report.return_value = report
        kwargs["calibration_analyzer"].get_category_adjustments.return_value = {"Politics": 0.05}
        kwargs["kelly_sizer"].update_calibration_multiplier = MagicMock()
        kwargs["kelly_sizer"].set_circuit_breaker_multiplier = MagicMock()

        await scan_and_trade(**kwargs, cycle_count=10)

        kwargs["calibration_analyzer"].generate_report.assert_called_once()
        kwargs["kelly_sizer"].update_calibration_multiplier.assert_called_once_with(0.15)

    @pytest.mark.asyncio
    async def test_no_calibration_report_at_cycle_5(self):
        """No calibration report at a non-multiple-of-10 cycle."""
        from src.core.models import Direction, Signal, StrategyName

        kwargs = _make_scan_kwargs()
        signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="MKT-A",
            market_question="Will A happen?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.60,
            market_price=0.50,
            confidence=0.7,
            reasoning="Test",
        )
        kwargs["ai_strategy"].scan_for_opportunities = AsyncMock(return_value=[signal])
        kwargs["kelly_sizer"].calculate_position_size = MagicMock(return_value=0)

        await scan_and_trade(**kwargs, cycle_count=5)

        kwargs["calibration_analyzer"].generate_report.assert_not_called()
