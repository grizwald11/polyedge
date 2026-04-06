"""Tests for orchestrator trade_cycle — exit processing and signal execution."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from src.core.models import (
    Direction,
    Market,
    MarketCategory,
    MarketToken,
    Order,
    OrderStatus,
    OrderType,
    Platform,
    Position,
    Side,
    Signal,
    StrategyName,
    TokenOutcome,
    Trade,
)
from src.orchestrator.trade_cycle import _execute_signals, _process_exits


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture
def sample_market():
    return Market(
        ticker="FED-RATE-CUT-MAY26",
        question="Will the Fed cut rates?",
        category=MarketCategory.FED_MACRO,
        tokens=[
            MarketToken(token_id="FED-CUT-YES", outcome=TokenOutcome.YES, price=0.34),
            MarketToken(token_id="FED-CUT-NO", outcome=TokenOutcome.NO, price=0.66),
        ],
        end_date=datetime.now(timezone.utc) + timedelta(days=45),
        volume_24h=125000.0,
        liquidity=45000.0,
        active=True,
    )


@pytest.fixture
def sample_position():
    return Position(
        market_id="FED-RATE-CUT-MAY26",
        token_id="FED-CUT-YES",
        direction=Direction.BUY_YES,
        size=10,
        avg_entry_price=0.30,
        current_price=0.34,
        unrealized_pnl=0.40,
        strategy=StrategyName.AI_PROBABILITY,
        paper=True,
        opened_at=datetime.now(timezone.utc) - timedelta(days=2),
    )


@pytest.fixture
def sample_signal():
    return Signal(
        strategy=StrategyName.AI_PROBABILITY,
        market_id="FED-RATE-CUT-MAY26",
        market_question="Will the Fed cut rates?",
        direction=Direction.BUY_YES,
        edge=0.08,
        probability_estimate=0.42,
        market_price=0.34,
        confidence=0.7,
        reasoning="Strong edge",
    )


@pytest.fixture
def logger():
    return logging.getLogger("test.trade_cycle")


def _make_exit_mocks(sample_market, sample_position):
    """Build mock objects needed by _process_exits."""
    position_manager = MagicMock()
    position_manager.get_all_positions.return_value = [sample_position]
    position_manager.get_exit_candidates.return_value = [
        (sample_position, "stop_loss: -30%")
    ]
    position_manager.has_pending_exit.return_value = False
    position_manager.get_total_unrealized_pnl.return_value = -5.0

    market_lookup = {sample_market.ticker: sample_market}

    scanner = MagicMock()
    scanner.db.has_recent_exit.return_value = False
    scanner.db.log_exit_reason = MagicMock()

    settings = MagicMock()
    settings.trading.mode = "paper"
    settings.trading.prefer_maker = True
    settings.alerts.alert_on_trade = False

    order_builder = MagicMock()
    order_builder._generate_order_id.return_value = "exit-order-001"

    trade = Trade(
        order_id="exit-order-001",
        market_id="FED-RATE-CUT-MAY26",
        token_id="FED-CUT-YES",
        side=Side.SELL,
        price=0.34,
        size=10,
        realized_pnl=0.40,
        paper=True,
    )
    order_result = MagicMock()
    order_result.success = True
    order_result.trade = trade
    order_router = AsyncMock()
    order_router.route_order = AsyncMock(return_value=order_result)

    circuit_breaker = MagicMock()
    circuit_breaker.is_halted.return_value = False

    fill_tracker = MagicMock()
    risk_engine = MagicMock()
    alert_manager = AsyncMock()
    metrics = MagicMock()
    kalshi = AsyncMock()

    return dict(
        position_manager=position_manager,
        market_lookup=market_lookup,
        scanner=scanner,
        settings=settings,
        kalshi=kalshi,
        poly_scanner=None,
        order_builder=order_builder,
        order_router=order_router,
        circuit_breaker=circuit_breaker,
        fill_tracker=fill_tracker,
        risk_engine=risk_engine,
        alert_manager=alert_manager,
        metrics=metrics,
        logger=logging.getLogger("test"),
    )


# ──────────────────────────────────────────────
# _process_exits tests
# ──────────────────────────────────────────────

class TestProcessExits:
    @pytest.mark.asyncio
    async def test_successful_exit(self, sample_market, sample_position):
        mocks = _make_exit_mocks(sample_market, sample_position)

        await _process_exits(**mocks)

        mocks["order_router"].route_order.assert_awaited_once()
        mocks["position_manager"].update_from_trade.assert_called_once()
        mocks["risk_engine"].record_exit.assert_called_once()
        call_args = mocks["risk_engine"].record_exit.call_args
        assert call_args[0][0] == "FED-RATE-CUT-MAY26"
        assert call_args[1]["pnl"] == 0.4
        assert "exit_reason" in call_args[1]
        mocks["position_manager"].clear_pending_exit.assert_called_once_with("FED-RATE-CUT-MAY26")

    @pytest.mark.asyncio
    async def test_skips_exit_if_pending(self, sample_market, sample_position):
        mocks = _make_exit_mocks(sample_market, sample_position)
        mocks["position_manager"].has_pending_exit.return_value = True

        await _process_exits(**mocks)

        mocks["order_router"].route_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_exit_if_recent_exit_exists(self, sample_market, sample_position):
        mocks = _make_exit_mocks(sample_market, sample_position)
        mocks["scanner"].db.has_recent_exit.return_value = True

        await _process_exits(**mocks)

        mocks["order_router"].route_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_exit_for_unknown_market(self, sample_market, sample_position):
        mocks = _make_exit_mocks(sample_market, sample_position)
        mocks["market_lookup"] = {}  # Market not in lookup

        await _process_exits(**mocks)

        mocks["order_router"].route_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_non_stop_loss_exits(self, sample_market, sample_position):
        mocks = _make_exit_mocks(sample_market, sample_position)
        mocks["circuit_breaker"].is_halted.return_value = True
        # Change exit reason to something that's not stop_loss
        mocks["position_manager"].get_exit_candidates.return_value = [
            (sample_position, "edge_gone: remaining edge < 20%")
        ]

        await _process_exits(**mocks)

        mocks["order_router"].route_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_circuit_breaker_allows_stop_loss_exits(self, sample_market, sample_position):
        mocks = _make_exit_mocks(sample_market, sample_position)
        mocks["circuit_breaker"].is_halted.return_value = True
        # stop_loss in the reason
        mocks["position_manager"].get_exit_candidates.return_value = [
            (sample_position, "stop_loss: -30%")
        ]

        await _process_exits(**mocks)

        mocks["order_router"].route_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_exit_if_price_zero(self, sample_market, sample_position):
        mocks = _make_exit_mocks(sample_market, sample_position)
        # Set market YES price to 0
        zero_market = Market(
            ticker="FED-RATE-CUT-MAY26",
            question="Will the Fed cut rates?",
            category=MarketCategory.FED_MACRO,
            tokens=[
                MarketToken(token_id="FED-CUT-YES", outcome=TokenOutcome.YES, price=0.0),
                MarketToken(token_id="FED-CUT-NO", outcome=TokenOutcome.NO, price=0.0),
            ],
            volume_24h=125000.0,
            active=True,
        )
        mocks["market_lookup"] = {"FED-RATE-CUT-MAY26": zero_market}

        await _process_exits(**mocks)

        mocks["order_router"].route_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resting_exit_order_tracked(self, sample_market, sample_position):
        """When route_order returns success but no trade, the order should be tracked."""
        mocks = _make_exit_mocks(sample_market, sample_position)
        result = MagicMock()
        result.success = True
        result.trade = None  # Order resting, not immediately filled
        mocks["order_router"].route_order = AsyncMock(return_value=result)

        await _process_exits(**mocks)

        mocks["fill_tracker"].track.assert_called_once()
        mocks["position_manager"].mark_pending_exit.assert_called_once_with("FED-RATE-CUT-MAY26")

    @pytest.mark.asyncio
    async def test_no_exits_when_no_candidates(self, sample_market, sample_position):
        mocks = _make_exit_mocks(sample_market, sample_position)
        mocks["position_manager"].get_exit_candidates.return_value = []

        await _process_exits(**mocks)

        mocks["order_router"].route_order.assert_not_awaited()


# ──────────────────────────────────────────────
# _execute_signals tests
# ──────────────────────────────────────────────

def _make_signal_mocks(sample_market, sample_signal):
    """Build mock objects needed by _execute_signals."""
    markets = [sample_market]
    market_lookup = {sample_market.ticker: sample_market}

    scanner = MagicMock()
    scanner.db.has_recent_trade.return_value = False
    scanner.db.log_signal.return_value = "sig-001"
    scanner.db.update_signal_risk_result = MagicMock()
    scanner.db.update_signal_acted_on = MagicMock()

    settings = MagicMock()
    settings.trading.prefer_maker = True
    settings.trading.max_trades_per_cycle = 5
    settings.alerts.alert_on_trade = False

    kelly_sizer = MagicMock()
    kelly_sizer.calculate_position_size.return_value = 10

    circuit_breaker = MagicMock()

    order = Order(
        id="order-001",
        market_id="FED-RATE-CUT-MAY26",
        token_id="FED-CUT-YES",
        side=Side.BUY,
        price=0.34,
        size=10,
        cost=3.40,
        order_type=OrderType.GTC,
        strategy=StrategyName.AI_PROBABILITY,
        paper=True,
    )
    order_builder = MagicMock()
    order_builder.build_limit_order.return_value = order
    order_builder.build_market_order.return_value = order

    trade = Trade(
        order_id="order-001",
        market_id="FED-RATE-CUT-MAY26",
        token_id="FED-CUT-YES",
        side=Side.BUY,
        price=0.34,
        size=10,
        realized_pnl=0.0,
        paper=True,
    )
    order_result = MagicMock()
    order_result.success = True
    order_result.trade = trade
    order_router = AsyncMock()
    order_router.route_order = AsyncMock(return_value=order_result)

    from src.core.models import RiskCheckResult
    risk_result = RiskCheckResult(passed=True)
    risk_engine = MagicMock()
    risk_engine.check_all.return_value = risk_result

    position_manager = MagicMock()
    position_manager.get_total_exposure.return_value = 50.0
    position_manager.update_from_trade = MagicMock()

    calibration = MagicMock()
    fill_tracker = MagicMock()
    alert_manager = AsyncMock()
    metrics = MagicMock()

    return dict(
        all_signals=[sample_signal],
        ai_signals=[sample_signal],
        no_signals=[],
        markets=markets,
        market_lookup=market_lookup,
        scanner=scanner,
        settings=settings,
        bankroll=500.0,
        kelly_sizer=kelly_sizer,
        circuit_breaker=circuit_breaker,
        order_builder=order_builder,
        order_router=order_router,
        risk_engine=risk_engine,
        position_manager=position_manager,
        calibration=calibration,
        fill_tracker=fill_tracker,
        alert_manager=alert_manager,
        logger=logging.getLogger("test"),
        metrics=metrics,
    )


class TestExecuteSignals:
    @pytest.mark.asyncio
    async def test_successful_trade_execution(self, sample_market, sample_signal):
        mocks = _make_signal_mocks(sample_market, sample_signal)

        result = await _execute_signals(**mocks)

        assert result == 1
        mocks["order_router"].route_order.assert_awaited_once()
        mocks["position_manager"].update_from_trade.assert_called_once()
        mocks["calibration"].log_prediction.assert_called_once()
        mocks["scanner"].db.update_signal_acted_on.assert_called_once_with("sig-001", "order-001")

    @pytest.mark.asyncio
    async def test_risk_engine_blocks_trade(self, sample_market, sample_signal):
        mocks = _make_signal_mocks(sample_market, sample_signal)
        from src.core.models import RiskCheckResult
        mocks["risk_engine"].check_all.return_value = RiskCheckResult(
            passed=False, failed_checks=["balance_insufficient"]
        )

        result = await _execute_signals(**mocks)

        assert result == 0
        mocks["order_router"].route_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kelly_sizes_to_zero_skips(self, sample_market, sample_signal):
        mocks = _make_signal_mocks(sample_market, sample_signal)
        mocks["kelly_sizer"].calculate_position_size.return_value = 0

        result = await _execute_signals(**mocks)

        assert result == 0
        mocks["order_router"].route_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recent_trade_dedup(self, sample_market, sample_signal):
        mocks = _make_signal_mocks(sample_market, sample_signal)
        mocks["scanner"].db.has_recent_trade.return_value = True

        result = await _execute_signals(**mocks)

        assert result == 0
        mocks["order_router"].route_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_market_skipped(self, sample_market, sample_signal):
        mocks = _make_signal_mocks(sample_market, sample_signal)
        mocks["market_lookup"] = {}  # Signal refers to unknown market

        result = await _execute_signals(**mocks)

        assert result == 0

    @pytest.mark.asyncio
    async def test_order_builder_returns_none_skipped(self, sample_market, sample_signal):
        mocks = _make_signal_mocks(sample_market, sample_signal)
        mocks["order_builder"].build_limit_order.return_value = None

        result = await _execute_signals(**mocks)

        assert result == 0

    @pytest.mark.asyncio
    async def test_max_trades_per_cycle_respected(self, sample_market):
        """Ensure trades stop after max_trades_per_cycle."""
        signals = []
        for i in range(10):
            sig = Signal(
                strategy=StrategyName.AI_PROBABILITY,
                market_id=f"MARKET-{i}",
                market_question=f"Market {i}?",
                direction=Direction.BUY_YES,
                edge=0.10 - i * 0.005,
                probability_estimate=0.50,
                market_price=0.40,
                confidence=0.7,
            )
            signals.append(sig)

        mocks = _make_signal_mocks(sample_market, signals[0])
        mocks["all_signals"] = signals
        mocks["ai_signals"] = signals
        mocks["settings"].trading.max_trades_per_cycle = 3

        # Add all markets to lookup
        for sig in signals:
            mocks["market_lookup"][sig.market_id] = Market(
                ticker=sig.market_id,
                question=sig.market_question,
                category=MarketCategory.FED_MACRO,
                tokens=[
                    MarketToken(token_id=f"{sig.market_id}_yes", outcome=TokenOutcome.YES, price=0.40),
                    MarketToken(token_id=f"{sig.market_id}_no", outcome=TokenOutcome.NO, price=0.60),
                ],
                volume_24h=50000.0,
                active=True,
            )

        result = await _execute_signals(**mocks)

        assert result <= 3

    @pytest.mark.asyncio
    async def test_signal_deconfliction_same_direction(self, sample_market):
        """When two strategies agree on direction, pick strongest."""
        sig1 = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="FED-RATE-CUT-MAY26",
            market_question="Will the Fed cut rates?",
            direction=Direction.BUY_YES,
            edge=0.05,
            probability_estimate=0.39,
            market_price=0.34,
            confidence=0.6,
        )
        sig2 = Signal(
            strategy=StrategyName.NEWS_REACTIVE,
            market_id="FED-RATE-CUT-MAY26",
            market_question="Will the Fed cut rates?",
            direction=Direction.BUY_YES,
            edge=0.10,  # Higher edge
            probability_estimate=0.44,
            market_price=0.34,
            confidence=0.8,
        )

        mocks = _make_signal_mocks(sample_market, sig1)
        mocks["all_signals"] = [sig1, sig2]
        mocks["ai_signals"] = [sig1]
        mocks["no_signals"] = []

        result = await _execute_signals(**mocks)

        assert result == 1
        # Only one trade, the stronger signal
        mocks["order_router"].route_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_signal_deconfliction_contradictory_skipped(self, sample_market):
        """When strategies disagree on direction, skip that market."""
        sig_yes = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="FED-RATE-CUT-MAY26",
            market_question="Will the Fed cut rates?",
            direction=Direction.BUY_YES,
            edge=0.08,
            probability_estimate=0.42,
            market_price=0.34,
            confidence=0.7,
        )
        sig_no = Signal(
            strategy=StrategyName.WHALE_TRACKER,
            market_id="FED-RATE-CUT-MAY26",
            market_question="Will the Fed cut rates?",
            direction=Direction.BUY_NO,
            edge=0.06,
            probability_estimate=0.28,
            market_price=0.34,
            confidence=0.65,
        )

        mocks = _make_signal_mocks(sample_market, sig_yes)
        mocks["all_signals"] = [sig_yes, sig_no]
        mocks["ai_signals"] = [sig_yes]
        mocks["no_signals"] = []

        result = await _execute_signals(**mocks)

        assert result == 0

    @pytest.mark.asyncio
    async def test_obvious_no_gets_separate_slots(self, sample_market):
        """Obvious NO signals get their own trade slots, not competing with edge signals."""
        edge_sig = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="MARKET-EDGE",
            market_question="Edge market?",
            direction=Direction.BUY_YES,
            edge=0.10,
            probability_estimate=0.50,
            market_price=0.40,
            confidence=0.7,
        )
        no_sig = Signal(
            strategy=StrategyName.OBVIOUS_NO,
            market_id="MARKET-NO",
            market_question="Absurd market?",
            direction=Direction.BUY_NO,
            edge=0.03,
            probability_estimate=0.02,
            market_price=0.03,
            confidence=0.95,
        )

        mocks = _make_signal_mocks(sample_market, edge_sig)
        mocks["all_signals"] = [edge_sig, no_sig]
        mocks["ai_signals"] = [edge_sig]
        mocks["no_signals"] = [no_sig]
        mocks["settings"].trading.max_trades_per_cycle = 5

        # Add both markets to lookup
        for sig in [edge_sig, no_sig]:
            mocks["market_lookup"][sig.market_id] = Market(
                ticker=sig.market_id,
                question=sig.market_question,
                category=MarketCategory.FED_MACRO,
                tokens=[
                    MarketToken(token_id=f"{sig.market_id}_yes", outcome=TokenOutcome.YES, price=0.40),
                    MarketToken(token_id=f"{sig.market_id}_no", outcome=TokenOutcome.NO, price=0.60),
                ],
                volume_24h=50000.0,
                active=True,
            )

        result = await _execute_signals(**mocks)

        # Both should execute (one edge + one obvious_no)
        assert result == 2

    @pytest.mark.asyncio
    async def test_resting_order_tracked_when_no_immediate_fill(self, sample_market, sample_signal):
        """When route_order succeeds but trade is None, order is tracked for fills."""
        mocks = _make_signal_mocks(sample_market, sample_signal)
        result = MagicMock()
        result.success = True
        result.trade = None
        mocks["order_router"].route_order = AsyncMock(return_value=result)

        executed = await _execute_signals(**mocks)

        # Not counted as a trade (no fill yet), but order is tracked
        assert executed == 0
        mocks["fill_tracker"].track.assert_called_once()

    @pytest.mark.asyncio
    async def test_metrics_tracked_for_signals(self, sample_market, sample_signal):
        mocks = _make_signal_mocks(sample_market, sample_signal)

        await _execute_signals(**mocks)

        mocks["metrics"].record_signal_generated.assert_called()
        mocks["metrics"].record_signal_executed.assert_called()

    @pytest.mark.asyncio
    async def test_metrics_record_risk_gated(self, sample_market, sample_signal):
        mocks = _make_signal_mocks(sample_market, sample_signal)
        from src.core.models import RiskCheckResult
        mocks["risk_engine"].check_all.return_value = RiskCheckResult(
            passed=False, failed_checks=["exposure_limit"]
        )

        await _execute_signals(**mocks)

        mocks["metrics"].record_signal_risk_gated.assert_called_once()

    @pytest.mark.asyncio
    async def test_calibration_buy_no_flips_probability(self, sample_market):
        """When direction is BUY_NO, calibration should use flipped probability."""
        sig = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="FED-RATE-CUT-MAY26",
            market_question="Will the Fed cut rates?",
            direction=Direction.BUY_NO,
            edge=0.08,
            probability_estimate=0.42,
            market_price=0.34,
            confidence=0.7,
        )

        mocks = _make_signal_mocks(sample_market, sig)
        mocks["all_signals"] = [sig]
        mocks["ai_signals"] = [sig]

        await _execute_signals(**mocks)

        # Calibration should be called with flipped values
        call_kwargs = mocks["calibration"].log_prediction.call_args
        assert call_kwargs is not None
        # 1 - 0.42 = 0.58
        assert abs(call_kwargs.kwargs.get("predicted_probability", call_kwargs[1]["predicted_probability"]) - 0.58) < 0.01


class TestFreshPriceInKellySizing:
    """Kelly sizing and order building must use fresh market price, not stale signal price."""

    @pytest.mark.asyncio
    async def test_kelly_uses_fresh_market_price_not_signal_price(self, sample_market, sample_signal):
        """Kelly sizer should receive the current market price, not the stale signal.market_price."""
        # Signal was generated with stale price 0.34
        sample_signal.market_price = 0.34
        # But market has since moved to 0.40 (yes) / 0.60 (no)
        sample_market.tokens[0].price = 0.40  # YES token
        sample_market.tokens[1].price = 0.60  # NO token

        mocks = _make_signal_mocks(sample_market, sample_signal)
        mocks["all_signals"] = [sample_signal]
        mocks["ai_signals"] = [sample_signal]

        await _execute_signals(**mocks)

        # Kelly sizer should have been called with fresh price 0.40, not stale 0.34
        kelly_call = mocks["kelly_sizer"].calculate_position_size.call_args
        assert kelly_call is not None
        assert kelly_call.kwargs.get("order_price", kelly_call[1].get("order_price")) == 0.40

    @pytest.mark.asyncio
    async def test_order_built_with_fresh_price(self, sample_market, sample_signal):
        """Order builder should receive the fresh market price for limit orders."""
        sample_signal.market_price = 0.34
        sample_market.tokens[0].price = 0.40
        sample_market.tokens[1].price = 0.60

        mocks = _make_signal_mocks(sample_market, sample_signal)
        mocks["all_signals"] = [sample_signal]
        mocks["ai_signals"] = [sample_signal]

        await _execute_signals(**mocks)

        # build_limit_order should receive fresh price 0.40
        build_call = mocks["order_builder"].build_limit_order.call_args
        assert build_call is not None
        # 4th positional arg is price
        assert build_call[0][3] == 0.40

    @pytest.mark.asyncio
    async def test_buy_no_uses_no_price(self, sample_market):
        """BUY_NO signals should use the fresh NO token price."""
        sig = Signal(
            strategy=StrategyName.AI_PROBABILITY,
            market_id="FED-RATE-CUT-MAY26",
            market_question="Will the Fed cut rates?",
            direction=Direction.BUY_NO,
            edge=0.08,
            probability_estimate=0.42,
            market_price=0.60,  # Stale
            confidence=0.7,
        )
        sample_market.tokens[0].price = 0.35  # YES moved
        sample_market.tokens[1].price = 0.65  # NO moved

        mocks = _make_signal_mocks(sample_market, sig)
        mocks["all_signals"] = [sig]
        mocks["ai_signals"] = [sig]

        await _execute_signals(**mocks)

        kelly_call = mocks["kelly_sizer"].calculate_position_size.call_args
        assert kelly_call is not None
        assert kelly_call.kwargs.get("order_price", kelly_call[1].get("order_price")) == 0.65
