"""End-to-end integration test — full paper trade cycle.

Runs the complete scan → assess → risk check → size → build → route pipeline
with mocked external APIs (Kalshi, Claude) but real internal components.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.alerts.alert_manager import AlertManager, LogBackend
from src.analysis.calibration import CalibrationTracker
from src.analysis.calibration_analyzer import CalibrationAnalyzer
from src.analysis.claude_forecaster import ClaudeForecaster
from src.analysis.resolution_tracker import ResolutionTracker
from src.config import Settings
from src.core.models import (
    Direction,
    ForecastResult,
    Market,
    MarketCategory,
    MarketToken,
    Side,
    Signal,
    StrategyName,
    Trade,
)
from src.data.market_scanner import MarketScanner
from src.execution.fill_tracker import FillTracker
from src.execution.order_builder import OrderBuilder
from src.execution.order_router import OrderRouter
from src.execution.position_manager import PositionManager
from src.main import scan_and_trade
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.kelly_sizer import KellySizer
from src.risk.risk_engine import RiskEngine
from src.storage.database import Database


def _make_markets() -> list[Market]:
    """Create a set of realistic test markets."""
    now = datetime.now(timezone.utc)
    return [
        Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Will the Federal Reserve cut rates at the May 2026 meeting?",
            description="Resolves YES if the FOMC announces a rate cut.",
            category=MarketCategory.FED_MACRO,
            tags=["Fed", "Interest Rates"],
            tokens=[
                MarketToken(token_id="FED-RATE-CUT-MAY26_yes", outcome="Yes", price=0.34),
                MarketToken(token_id="FED-RATE-CUT-MAY26_no", outcome="No", price=0.66),
            ],
            end_date=now + timedelta(days=45),
            volume_24h=125000.0,
            volume_total=3500000.0,
            liquidity=45000.0,
            spread=0.02,
            active=True,
        ),
        Market(
            ticker="TRUMP-APPROVAL-50",
            question="Will Trump approval rating exceed 50% by June 2026?",
            category=MarketCategory.POLITICS,
            tags=["Politics", "Trump"],
            tokens=[
                MarketToken(token_id="TRUMP-APPROVAL-50_yes", outcome="Yes", price=0.22),
                MarketToken(token_id="TRUMP-APPROVAL-50_no", outcome="No", price=0.78),
            ],
            end_date=now + timedelta(days=90),
            volume_24h=80000.0,
            volume_total=2000000.0,
            liquidity=30000.0,
            spread=0.03,
            active=True,
        ),
        # Low-volume market — should be filtered or produce smaller signal
        Market(
            ticker="AI-BENCHMARK",
            question="Will an AI model score 90%+ on ARC-AGI by end of 2026?",
            category=MarketCategory.TECH_AI,
            tags=["AI", "Benchmarks"],
            tokens=[
                MarketToken(token_id="AI-BENCHMARK_yes", outcome="Yes", price=0.60),
                MarketToken(token_id="AI-BENCHMARK_no", outcome="No", price=0.40),
            ],
            end_date=now + timedelta(days=280),
            volume_24h=15000.0,
            liquidity=8000.0,
            active=True,
        ),
        # Near-certain NO market for obvious_no strategy
        Market(
            ticker="ALIENS-2026",
            question="Will aliens make verified contact with Earth in 2026?",
            category=MarketCategory.OTHER,
            tags=["Science"],
            tokens=[
                MarketToken(token_id="ALIENS-2026_yes", outcome="Yes", price=0.02),
                MarketToken(token_id="ALIENS-2026_no", outcome="No", price=0.98),
            ],
            end_date=now + timedelta(days=20),
            volume_24h=25000.0,
            liquidity=10000.0,
            active=True,
        ),
    ]


def _mock_forecast(probability: float = 0.42) -> ForecastResult:
    return ForecastResult(
        probability=probability,
        confidence_low=probability - 0.07,
        confidence_high=probability + 0.08,
        key_factors_for=["Factor A"],
        key_factors_against=["Factor B"],
        uncertainties=["Uncertainty"],
        reasoning="Test reasoning",
        model_used="claude-sonnet-4-6",
        tokens_used=500,
        latency_ms=1000,
    )


@pytest.fixture
def integration_settings() -> Settings:
    """Settings tuned for integration testing."""
    return Settings(
        trading={"mode": "paper", "bankroll": 500.0, "min_edge_ai": 0.05},
        claude={"cross_check_enabled": False},
    )


@pytest.fixture
def integration_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "integration.db"), wal_mode=True)
    conn = db._get_conn()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.commit()
    return db


class TestFullPaperTradeCycle:
    """End-to-end paper trade cycle tests."""

    @pytest.mark.asyncio
    async def test_scan_assess_trade_cycle(self, integration_db, integration_settings):
        """Full pipeline: scan → Claude assess → risk check → Kelly size → paper trade."""
        db = integration_db
        settings = integration_settings
        markets = _make_markets()

        # Mock scanner to return our test markets
        mock_discovery = AsyncMock()
        scanner = MarketScanner(mock_discovery, db, settings)
        scanner.run_scan_cycle = AsyncMock(return_value=markets)

        # Mock forecaster — returns 42% for Fed market (8% edge over 34% market price)
        forecaster = ClaudeForecaster(settings)
        forecaster.assess_market = AsyncMock(return_value=_mock_forecast(0.42))

        calibration = CalibrationTracker(db)
        calibration_analyzer = CalibrationAnalyzer(db)
        resolution_tracker = MagicMock()
        resolution_tracker.check_resolutions = AsyncMock(return_value=0)

        # Real components
        from src.strategies.ai_probability import AIProbabilityStrategy
        from src.strategies.obvious_no import ObviousNoStrategy

        ai_strategy = AIProbabilityStrategy(forecaster, settings, db, calibration_analyzer)
        no_strategy = ObviousNoStrategy(settings)

        order_builder = OrderBuilder(settings)
        mock_kalshi = AsyncMock()
        # check_key_freshness is called synchronously (no await) in scan_and_trade,
        # so it must be a regular MagicMock to avoid unawaited coroutine warnings.
        mock_kalshi.check_key_freshness = MagicMock(return_value=True)
        order_router = OrderRouter(settings, mock_kalshi, db)
        position_manager = PositionManager(db, settings.trading.bankroll)
        fill_tracker = FillTracker(mock_kalshi, db)
        alert_manager = AlertManager()
        alert_manager.register(LogBackend())

        circuit_breaker = CircuitBreaker(settings, db)
        kelly_sizer = KellySizer(settings)
        risk_engine = RiskEngine(settings, position_manager, circuit_breaker, db)

        # Run one full cycle
        await scan_and_trade(
            scanner=scanner,
            kalshi=mock_kalshi,
            ai_strategy=ai_strategy,
            no_strategy=no_strategy,
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
            calibration=calibration,
            resolution_tracker=resolution_tracker,
            calibration_analyzer=calibration_analyzer,
            fill_tracker=fill_tracker,
            alert_manager=alert_manager,
            metrics=None,
            settings=settings,
            cycle_count=1,
        )

        # Verify trades were executed
        positions = position_manager.get_all_positions()
        assert len(positions) >= 1, "Expected at least 1 position from AI or obvious_no strategy"

        # Verify trades in DB
        stats = db.get_stats()
        assert stats["total_trades"] >= 1
        assert stats["total_signals"] >= 1

        # Verify calibration was logged
        unresolved = db.get_unresolved_predictions()
        assert len(unresolved) >= 1

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_trading(self, integration_db, integration_settings):
        """Circuit breaker halts all trading when triggered."""
        db = integration_db
        settings = integration_settings
        markets = _make_markets()

        mock_discovery = AsyncMock()
        scanner = MarketScanner(mock_discovery, db, settings)
        scanner.run_scan_cycle = AsyncMock(return_value=markets)

        forecaster = ClaudeForecaster(settings)
        forecaster.assess_market = AsyncMock(return_value=_mock_forecast(0.42))

        calibration = CalibrationTracker(db)
        calibration_analyzer = CalibrationAnalyzer(db)
        resolution_tracker = MagicMock()
        resolution_tracker.check_resolutions = AsyncMock(return_value=0)

        from src.strategies.ai_probability import AIProbabilityStrategy
        from src.strategies.obvious_no import ObviousNoStrategy

        ai_strategy = AIProbabilityStrategy(forecaster, settings, db, calibration_analyzer)
        no_strategy = ObviousNoStrategy(settings)

        order_builder = OrderBuilder(settings)
        mock_kalshi = AsyncMock()
        # check_key_freshness is called synchronously (no await) in scan_and_trade,
        # so it must be a regular MagicMock to avoid unawaited coroutine warnings.
        mock_kalshi.check_key_freshness = MagicMock(return_value=True)
        order_router = OrderRouter(settings, mock_kalshi, db)
        position_manager = PositionManager(db, settings.trading.bankroll)
        fill_tracker = FillTracker(mock_kalshi, db)
        alert_manager = AlertManager()
        alert_manager.register(LogBackend())

        circuit_breaker = CircuitBreaker(settings, db)
        kelly_sizer = KellySizer(settings)
        risk_engine = RiskEngine(settings, position_manager, circuit_breaker, db)

        # Force circuit breaker to halt
        circuit_breaker._halted = True
        circuit_breaker._halt_reason = "Daily loss limit exceeded"

        await scan_and_trade(
            scanner=scanner,
            kalshi=mock_kalshi,
            ai_strategy=ai_strategy,
            no_strategy=no_strategy,
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
            calibration=calibration,
            resolution_tracker=resolution_tracker,
            calibration_analyzer=calibration_analyzer,
            fill_tracker=fill_tracker,
            alert_manager=alert_manager,
            metrics=None,
            settings=settings,
            cycle_count=1,
        )

        # No trades should have been executed
        assert position_manager.get_position_count() == 0
        assert db.get_stats()["total_trades"] == 0

    @pytest.mark.asyncio
    async def test_risk_engine_blocks_oversized_position(self, integration_db):
        """Risk engine blocks a position that exceeds max position size."""
        db = integration_db
        # Set tiny bankroll so any trade exceeds limits
        settings = Settings(
            trading={"mode": "paper", "bankroll": 10.0, "max_position_pct": 0.05},
        )
        markets = _make_markets()

        mock_discovery = AsyncMock()
        scanner = MarketScanner(mock_discovery, db, settings)
        scanner.run_scan_cycle = AsyncMock(return_value=markets)

        # Return huge edge to trigger large sizing attempt
        forecaster = ClaudeForecaster(settings)
        forecaster.assess_market = AsyncMock(return_value=_mock_forecast(0.90))

        calibration = CalibrationTracker(db)
        calibration_analyzer = CalibrationAnalyzer(db)
        resolution_tracker = MagicMock()
        resolution_tracker.check_resolutions = AsyncMock(return_value=0)

        from src.strategies.ai_probability import AIProbabilityStrategy
        from src.strategies.obvious_no import ObviousNoStrategy

        ai_strategy = AIProbabilityStrategy(forecaster, settings, db, calibration_analyzer)
        no_strategy = ObviousNoStrategy(settings)

        order_builder = OrderBuilder(settings)
        mock_kalshi = AsyncMock()
        # check_key_freshness is called synchronously (no await) in scan_and_trade,
        # so it must be a regular MagicMock to avoid unawaited coroutine warnings.
        mock_kalshi.check_key_freshness = MagicMock(return_value=True)
        order_router = OrderRouter(settings, mock_kalshi, db)
        position_manager = PositionManager(db, settings.trading.bankroll)
        fill_tracker = FillTracker(mock_kalshi, db)
        alert_manager = AlertManager()

        circuit_breaker = CircuitBreaker(settings, db)
        kelly_sizer = KellySizer(settings)
        risk_engine = RiskEngine(settings, position_manager, circuit_breaker, db)

        await scan_and_trade(
            scanner=scanner,
            kalshi=mock_kalshi,
            ai_strategy=ai_strategy,
            no_strategy=no_strategy,
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
            calibration=calibration,
            resolution_tracker=resolution_tracker,
            calibration_analyzer=calibration_analyzer,
            fill_tracker=fill_tracker,
            alert_manager=alert_manager,
            metrics=None,
            settings=settings,
            cycle_count=1,
        )

        # With $10 bankroll and 5% max, positions should be tiny or blocked
        exposure = position_manager.get_total_exposure()
        assert exposure <= settings.trading.bankroll * settings.trading.max_total_exposure_pct

    @pytest.mark.asyncio
    async def test_no_signals_no_trades(self, integration_db, integration_settings):
        """When Claude sees no edge, no trades are placed."""
        db = integration_db
        settings = integration_settings

        # Market price matches Claude's estimate — no edge
        markets = [
            Market(
                ticker="NO-EDGE",
                question="Some market?",
                category=MarketCategory.OTHER,
                tokens=[
                    MarketToken(token_id="NO-EDGE_yes", outcome="Yes", price=0.50),
                    MarketToken(token_id="NO-EDGE_no", outcome="No", price=0.50),
                ],
                end_date=datetime.now(timezone.utc) + timedelta(days=30),
                volume_24h=50000.0,
                liquidity=20000.0,
                active=True,
            ),
        ]

        mock_discovery = AsyncMock()
        scanner = MarketScanner(mock_discovery, db, settings)
        scanner.run_scan_cycle = AsyncMock(return_value=markets)

        # Claude agrees with market — 50% probability, 0% edge
        forecaster = ClaudeForecaster(settings)
        forecaster.assess_market = AsyncMock(return_value=_mock_forecast(0.50))

        calibration = CalibrationTracker(db)
        calibration_analyzer = CalibrationAnalyzer(db)
        resolution_tracker = MagicMock()
        resolution_tracker.check_resolutions = AsyncMock(return_value=0)

        from src.strategies.ai_probability import AIProbabilityStrategy
        from src.strategies.obvious_no import ObviousNoStrategy

        ai_strategy = AIProbabilityStrategy(forecaster, settings, db, calibration_analyzer)
        no_strategy = ObviousNoStrategy(settings)

        order_builder = OrderBuilder(settings)
        mock_kalshi = AsyncMock()
        # check_key_freshness is called synchronously (no await) in scan_and_trade,
        # so it must be a regular MagicMock to avoid unawaited coroutine warnings.
        mock_kalshi.check_key_freshness = MagicMock(return_value=True)
        order_router = OrderRouter(settings, mock_kalshi, db)
        position_manager = PositionManager(db, settings.trading.bankroll)
        fill_tracker = FillTracker(mock_kalshi, db)
        alert_manager = AlertManager()

        circuit_breaker = CircuitBreaker(settings, db)
        kelly_sizer = KellySizer(settings)
        risk_engine = RiskEngine(settings, position_manager, circuit_breaker, db)

        await scan_and_trade(
            scanner=scanner,
            kalshi=mock_kalshi,
            ai_strategy=ai_strategy,
            no_strategy=no_strategy,
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
            calibration=calibration,
            resolution_tracker=resolution_tracker,
            calibration_analyzer=calibration_analyzer,
            fill_tracker=fill_tracker,
            alert_manager=alert_manager,
            metrics=None,
            settings=settings,
            cycle_count=1,
        )

        assert position_manager.get_position_count() == 0
        assert db.get_stats()["total_trades"] == 0

    @pytest.mark.asyncio
    async def test_multiple_cycles_accumulate(self, integration_db, integration_settings):
        """Running multiple cycles accumulates positions correctly."""
        db = integration_db
        settings = integration_settings
        markets = _make_markets()

        mock_discovery = AsyncMock()
        scanner = MarketScanner(mock_discovery, db, settings)
        scanner.run_scan_cycle = AsyncMock(return_value=markets)

        forecaster = ClaudeForecaster(settings)
        forecaster.assess_market = AsyncMock(return_value=_mock_forecast(0.42))

        calibration = CalibrationTracker(db)
        calibration_analyzer = CalibrationAnalyzer(db)
        resolution_tracker = MagicMock()
        resolution_tracker.check_resolutions = AsyncMock(return_value=0)

        from src.strategies.ai_probability import AIProbabilityStrategy
        from src.strategies.obvious_no import ObviousNoStrategy

        ai_strategy = AIProbabilityStrategy(forecaster, settings, db, calibration_analyzer)
        no_strategy = ObviousNoStrategy(settings)

        order_builder = OrderBuilder(settings)
        mock_kalshi = AsyncMock()
        # check_key_freshness is called synchronously (no await) in scan_and_trade,
        # so it must be a regular MagicMock to avoid unawaited coroutine warnings.
        mock_kalshi.check_key_freshness = MagicMock(return_value=True)
        order_router = OrderRouter(settings, mock_kalshi, db)
        position_manager = PositionManager(db, settings.trading.bankroll)
        fill_tracker = FillTracker(mock_kalshi, db)
        alert_manager = AlertManager()

        circuit_breaker = CircuitBreaker(settings, db)
        kelly_sizer = KellySizer(settings)
        risk_engine = RiskEngine(settings, position_manager, circuit_breaker, db)

        kwargs = dict(
            scanner=scanner, kalshi=mock_kalshi, ai_strategy=ai_strategy, no_strategy=no_strategy,
            news_strategy=None, cross_arb_strategy=None, whale_strategy=None,
            market_graph=None, risk_engine=risk_engine, kelly_sizer=kelly_sizer,
            circuit_breaker=circuit_breaker, order_builder=order_builder,
            order_router=order_router, position_manager=position_manager,
            calibration=calibration, resolution_tracker=resolution_tracker,
            calibration_analyzer=calibration_analyzer, fill_tracker=fill_tracker,
            alert_manager=alert_manager, metrics=None, settings=settings,
        )

        # Run 3 cycles
        for i in range(3):
            await scan_and_trade(**kwargs, cycle_count=i + 1)

        # After 3 cycles, risk engine should prevent duplicate entries
        # (check_existing_position blocks re-entry on same market)
        positions = position_manager.get_all_positions()
        market_ids = [p.market_id for p in positions]
        # No duplicates
        assert len(market_ids) == len(set(market_ids))

    @pytest.mark.asyncio
    async def test_exit_logic_in_trading_loop(self, integration_db, integration_settings):
        """Positions that hit stop-loss are automatically closed in the next cycle."""
        db = integration_db
        settings = integration_settings
        markets = _make_markets()

        mock_discovery = AsyncMock()
        scanner = MarketScanner(mock_discovery, db, settings)
        scanner.run_scan_cycle = AsyncMock(return_value=markets)

        forecaster = ClaudeForecaster(settings)
        forecaster.assess_market = AsyncMock(return_value=_mock_forecast(0.42))

        calibration = CalibrationTracker(db)
        calibration_analyzer = CalibrationAnalyzer(db)
        resolution_tracker = MagicMock()
        resolution_tracker.check_resolutions = AsyncMock(return_value=0)

        from src.strategies.ai_probability import AIProbabilityStrategy
        from src.strategies.obvious_no import ObviousNoStrategy

        ai_strategy = AIProbabilityStrategy(forecaster, settings, db, calibration_analyzer)
        no_strategy = ObviousNoStrategy(settings)

        # Directly mock signal generation so this test doesn't depend on
        # AI pipeline internals (divergence gates, confidence checks, etc.).
        # This test is about EXIT logic, not signal generation.
        cycle1_signal = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="FED-RATE-CUT-MAY26",
            market_question="Will the Federal Reserve cut rates at the May 2026 meeting?",
            direction=Direction.BUY_YES,
            edge=0.08,
            probability_estimate=0.42,
            market_price=0.34,
            confidence=0.85,
            reasoning="Test signal for exit logic",
        )
        ai_strategy.scan_for_opportunities = AsyncMock(return_value=[cycle1_signal])

        order_builder = OrderBuilder(settings)
        mock_kalshi = AsyncMock()
        # check_key_freshness is called synchronously (no await) in scan_and_trade,
        # so it must be a regular MagicMock to avoid unawaited coroutine warnings.
        mock_kalshi.check_key_freshness = MagicMock(return_value=True)
        order_router = OrderRouter(settings, mock_kalshi, db)
        position_manager = PositionManager(db, settings.trading.bankroll)
        fill_tracker = FillTracker(mock_kalshi, db)
        alert_manager = AlertManager()
        alert_manager.register(LogBackend())

        circuit_breaker = CircuitBreaker(settings, db)
        kelly_sizer = KellySizer(settings)
        risk_engine = RiskEngine(settings, position_manager, circuit_breaker, db)

        kwargs = dict(
            scanner=scanner, kalshi=mock_kalshi, ai_strategy=ai_strategy, no_strategy=no_strategy,
            news_strategy=None, cross_arb_strategy=None, whale_strategy=None,
            market_graph=None, risk_engine=risk_engine, kelly_sizer=kelly_sizer,
            circuit_breaker=circuit_breaker, order_builder=order_builder,
            order_router=order_router, position_manager=position_manager,
            calibration=calibration, resolution_tracker=resolution_tracker,
            calibration_analyzer=calibration_analyzer, fill_tracker=fill_tracker,
            alert_manager=alert_manager, metrics=None, settings=settings,
        )

        # Cycle 1: open positions via directly-mocked AI signal
        await scan_and_trade(**kwargs, cycle_count=1)
        positions_after_open = position_manager.get_all_positions()
        assert len(positions_after_open) >= 1, "Expected at least 1 position opened"

        positions_before_exit = len(position_manager.get_all_positions())

        # Track which positions were opened so we can verify exits
        opened_market_ids = {p.market_id for p in position_manager.get_all_positions()}

        # Cycle 2: provide markets with crashed prices to trigger stop-loss.
        # The trading loop calls update_price() with market prices, so we need
        # the market data itself to reflect the crash.
        crashed_markets = _make_markets()
        for m in crashed_markets:
            for token in m.tokens:
                if token.outcome == "Yes":
                    token.price = 0.05  # Crashed YES price
                else:
                    token.price = 0.95
        scanner.run_scan_cycle = AsyncMock(return_value=crashed_markets)

        # Also make Claude return no edge so no NEW positions open
        forecaster.assess_market = AsyncMock(return_value=_mock_forecast(0.05))
        # Prevent obvious_no from opening new positions on crashed markets
        # (YES=0.05 qualifies as obvious NO candidate, masking exit results)
        no_strategy.generate_signals = MagicMock(return_value=[])

        await scan_and_trade(**kwargs, cycle_count=2)

        # Verify at least one of the original positions was closed
        remaining_ids = {p.market_id for p in position_manager.get_all_positions()}
        closed_ids = opened_market_ids - remaining_ids
        assert len(closed_ids) >= 1, (
            f"Expected at least 1 exit from {opened_market_ids}, "
            f"but all still open: {remaining_ids}"
        )

    @pytest.mark.asyncio
    async def test_max_trades_per_cycle_limits_execution(self, integration_db):
        """max_trades_per_cycle should cap how many trades execute per cycle."""
        db = integration_db
        # Set max_trades_per_cycle to 1 — only best signal should trade
        settings = Settings(
            trading={
                "mode": "paper", "bankroll": 500.0,
                "min_edge_ai": 0.05, "max_trades_per_cycle": 1,
            },
        )
        markets = _make_markets()

        mock_discovery = AsyncMock()
        scanner = MarketScanner(mock_discovery, db, settings)
        scanner.run_scan_cycle = AsyncMock(return_value=markets)

        forecaster = ClaudeForecaster(settings)
        # Return 42% — gives 8% edge over 34% market price (passes min_edge)
        forecaster.assess_market = AsyncMock(return_value=_mock_forecast(0.42))

        calibration = CalibrationTracker(db)
        calibration_analyzer = CalibrationAnalyzer(db)
        resolution_tracker = MagicMock()
        resolution_tracker.check_resolutions = AsyncMock(return_value=0)

        from src.strategies.ai_probability import AIProbabilityStrategy
        from src.strategies.obvious_no import ObviousNoStrategy

        ai_strategy = AIProbabilityStrategy(forecaster, settings, db, calibration_analyzer)
        no_strategy = ObviousNoStrategy(settings)

        order_builder = OrderBuilder(settings)
        mock_kalshi = AsyncMock()
        # check_key_freshness is called synchronously (no await) in scan_and_trade,
        # so it must be a regular MagicMock to avoid unawaited coroutine warnings.
        mock_kalshi.check_key_freshness = MagicMock(return_value=True)
        order_router = OrderRouter(settings, mock_kalshi, db)
        position_manager = PositionManager(db, settings.trading.bankroll)
        fill_tracker = FillTracker(mock_kalshi, db)
        alert_manager = AlertManager()

        circuit_breaker = CircuitBreaker(settings, db)
        kelly_sizer = KellySizer(settings)
        risk_engine = RiskEngine(settings, position_manager, circuit_breaker, db)

        await scan_and_trade(
            scanner=scanner, kalshi=mock_kalshi, ai_strategy=ai_strategy, no_strategy=no_strategy,
            news_strategy=None, cross_arb_strategy=None, whale_strategy=None,
            market_graph=None, risk_engine=risk_engine, kelly_sizer=kelly_sizer,
            circuit_breaker=circuit_breaker, order_builder=order_builder,
            order_router=order_router, position_manager=position_manager,
            calibration=calibration, resolution_tracker=resolution_tracker,
            calibration_analyzer=calibration_analyzer, fill_tracker=fill_tracker,
            alert_manager=alert_manager, metrics=None, settings=settings,
            cycle_count=1,
        )

        # Only 1 trade should have executed despite multiple signals
        assert position_manager.get_position_count() <= 1
        assert db.get_stats()["total_trades"] <= 1
