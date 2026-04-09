"""Risk engine -- 15-point pre-trade risk check.

Every trade must pass ALL checks before execution.

Individual check implementations live in :mod:`src.risk.risk_checks`;
this module contains the :class:`RiskEngine` orchestrator that wires
them together.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from src.config import Settings
from src.core.models import Market, RiskCheckResult, Signal
from src.execution.position_manager import PositionManager
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.manipulation_detector import ManipulationDetector
from src.risk.portfolio_risk import PortfolioRisk
from src.risk import risk_checks
from src.storage.database import Database

logger = logging.getLogger(__name__)

# Re-export for backward compatibility
WASH_TRADE_COOLDOWN_SECONDS = risk_checks.WASH_TRADE_COOLDOWN_SECONDS


class RiskEngine:
    """Central risk gate -- all trades must pass."""

    def __init__(
        self,
        settings: Settings,
        position_manager: PositionManager,
        circuit_breaker: CircuitBreaker,
        db: Database | None = None,
        portfolio_risk: PortfolioRisk | None = None,
        manipulation_detector: ManipulationDetector | None = None,
        correlation_detector=None,
    ):
        self.settings = settings
        self.positions = position_manager
        self.circuit_breaker = circuit_breaker
        self.db = db
        self.portfolio_risk = portfolio_risk
        self.correlation_detector = correlation_detector
        self.manipulation_detector = manipulation_detector or ManipulationDetector()
        self._bankroll_override: float | None = None  # Live-synced bankroll
        # Cooldown after exiting a position: longer for losses to avoid
        # re-entering bad positions, shorter for profitable exits.
        self.cooldown_loss_seconds = 14400   # 4 hours after a loss exit
        self.cooldown_profit_seconds = 3600  # 1 hour after a profit exit
        self.cooldown_edge_gone_seconds = 28800  # 8 hours after edge-gone exit
        self.cooldown_seconds = 3600  # Default for legacy/unknown exits
        self._cooldown_durations: dict[str, int] = {}  # market_id -> seconds
        # Load persisted cooldowns if DB available, otherwise start empty
        if db is not None:
            self._cooldowns: dict[str, datetime] = db.load_cooldowns(self.cooldown_seconds)
            self._cooldown_durations = db.load_cooldown_durations()
            logger.debug("Loaded %d active cooldowns from DB", len(self._cooldowns))
            if self._cooldowns:
                logger.info("Loaded %d active cooldowns from DB", len(self._cooldowns))
            elif self.positions.get_all_positions():
                logger.warning(
                    "DB returned 0 cooldowns but %d open positions exist — "
                    "cooldowns may have been lost (DB reset or first run after migration)",
                    len(self.positions.get_all_positions()),
                )
        else:
            self._cooldowns = {}

    @property
    def bankroll(self) -> float:
        """Current bankroll -- uses live-synced value if available, else config."""
        if self._bankroll_override is not None:
            return self._bankroll_override
        return self.settings.trading.bankroll

    def update_bankroll(self, live_balance: float) -> None:
        """Update bankroll from live balance sync. Persists to DB for crash recovery."""
        self._bankroll_override = live_balance
        if self.db is not None:
            try:
                self.db.save_setting("live_bankroll", str(live_balance))
            except (sqlite3.Error, OSError) as e:
                logger.debug(f"Failed to persist bankroll to DB: {e}")

    def restore_bankroll(self) -> None:
        """Restore live-synced bankroll from DB on startup."""
        if self.db is None:
            return
        try:
            stored = self.db.load_setting("live_bankroll")
            if stored is not None:
                self._bankroll_override = float(stored)
                logger.info(f"Restored live bankroll from DB: ${self._bankroll_override:.2f}")
        except (ValueError, TypeError) as e:
            logger.debug(f"Could not restore bankroll from DB: {e}")

    def check_all(
        self,
        signal: Signal,
        market: Market,
        proposed_size: float,
        proposed_cost: float,
        pending_order_cost: float = 0.0,
    ) -> RiskCheckResult:
        """Run all 15 risk checks on a proposed trade.

        Args:
            signal: The trading signal
            market: Market being traded
            proposed_size: Number of contracts
            proposed_cost: Total cost in dollars (price * size)
            pending_order_cost: Total cost of unfilled pending orders

        Returns:
            RiskCheckResult with pass/fail and details
        """
        failed: list[str] = []
        warnings: list[str] = []
        bankroll = self.bankroll

        risk_checks.check_excluded_category(self.settings, market, failed)
        committed = risk_checks.check_balance(
            self.positions, bankroll, proposed_cost, pending_order_cost, failed,
        )
        risk_checks.check_position_size(self.settings, bankroll, proposed_cost, failed)
        risk_checks.check_total_exposure(
            self.settings, bankroll, proposed_cost, committed, failed,
        )
        risk_checks.check_correlated_exposure(
            self.settings, self.positions, bankroll, signal, proposed_cost,
            failed, warnings,
            correlation_detector=self.correlation_detector,
            portfolio_risk=self.portfolio_risk,
        )
        risk_checks.check_circuit_breaker(self.circuit_breaker, failed)
        risk_checks.check_liquidity(market, proposed_cost, failed, warnings)
        risk_checks.check_existing_position(
            self.settings, self.positions, signal, failed, warnings,
            proposed_cost=proposed_cost, bankroll=bankroll,
        )
        risk_checks.check_signal_quality(self.settings, signal, proposed_cost, failed)
        risk_checks.check_resolution_date(market, failed, warnings)
        risk_checks.check_cooldown(
            signal.market_id, self._cooldowns, self._cooldown_durations,
            self.cooldown_seconds, failed, db=self.db,
        )
        risk_checks.check_wash_trade(signal.market_id, self.db, failed)
        risk_checks.check_manipulation(self.manipulation_detector, market, failed)
        risk_checks.check_strategy_exposure(
            self.settings, self.positions, signal, bankroll, proposed_cost, failed,
        )
        risk_checks.check_obvious_no_limit(
            self.settings, self.positions, signal, bankroll, proposed_cost, failed,
        )
        risk_checks.check_max_concurrent_positions(
            self.settings, self.positions, signal, failed,
        )
        risk_checks.check_spread_vs_edge(signal, market, failed)

        passed = len(failed) == 0
        result = RiskCheckResult(
            passed=passed,
            failed_checks=failed,
            warnings=warnings,
            approved_size=proposed_size if passed else 0.0,
        )

        if not passed:
            logger.info(
                f"Risk REJECTED {signal.market_id}: {', '.join(failed)}"
            )
        elif warnings:
            logger.info(
                f"Risk PASSED {signal.market_id} with warnings: {', '.join(warnings)}"
            )

        return result

    def record_exit(
        self, market_id: str, pnl: float = 0.0, exit_reason: str = "",
    ):
        """Record a position exit for cooldown tracking.

        Losses get a longer cooldown (4h) than profits (1h).
        Edge-gone exits get an extended cooldown (8h) to prevent
        repeatedly re-entering a market where the thesis is wrong.
        Also applies cooldown to sibling markets (same event_ticker)
        to prevent re-entering via a different expiry date.
        """
        now = datetime.now(timezone.utc)
        # Determine duration based on exit reason and P&L
        if "edge_gone" in exit_reason.lower():
            cd_duration = self.cooldown_edge_gone_seconds
        elif pnl < 0:
            cd_duration = self.cooldown_loss_seconds
        else:
            cd_duration = self.cooldown_profit_seconds

        # Apply to this market
        self._apply_cooldown(market_id, now, cd_duration)

        # Also apply to sibling markets (same event) to prevent
        # re-entering via a different expiry variant
        if self.db is not None and "edge_gone" in exit_reason.lower():
            try:
                conn = self.db._get_conn()
                row = conn.execute(
                    "SELECT event_ticker FROM markets WHERE ticker = ?",
                    (market_id,),
                ).fetchone()
                if row and row["event_ticker"]:
                    siblings = conn.execute(
                        "SELECT ticker FROM markets WHERE event_ticker = ? AND ticker != ?",
                        (row["event_ticker"], market_id),
                    ).fetchall()
                    for sib in siblings:
                        self._apply_cooldown(sib["ticker"], now, cd_duration)
                        logger.info(
                            f"Edge-gone cooldown extended to sibling {sib['ticker']} "
                            f"(event: {row['event_ticker']})"
                        )
            except Exception as e:
                logger.debug(f"Sibling cooldown lookup failed: {e}")

    def _apply_cooldown(self, market_id: str, now: datetime, duration: int):
        """Apply a cooldown to a single market_id."""
        if self.db is not None:
            self.db.save_cooldown(market_id, now, duration)
        self._cooldowns[market_id] = now
        self._cooldown_durations[market_id] = duration

    # -- Backward-compatible private method aliases --------------------------------
    # These delegate to the standalone functions in risk_checks.py so that any
    # code calling engine._check_xxx() (e.g., tests) continues to work.

    def _check_excluded_category(self, market, failed):
        return risk_checks.check_excluded_category(self.settings, market, failed)

    def _check_balance(self, bankroll, proposed_cost, pending_order_cost, failed):
        return risk_checks.check_balance(
            self.positions, bankroll, proposed_cost, pending_order_cost, failed,
        )

    def _check_position_size(self, bankroll, proposed_cost, failed):
        return risk_checks.check_position_size(self.settings, bankroll, proposed_cost, failed)

    def _check_total_exposure(self, bankroll, proposed_cost, committed, failed):
        return risk_checks.check_total_exposure(
            self.settings, bankroll, proposed_cost, committed, failed,
        )

    def _check_correlated_exposure(self, bankroll, signal, proposed_cost, failed, warnings):
        return risk_checks.check_correlated_exposure(
            self.settings, self.positions, bankroll, signal, proposed_cost,
            failed, warnings,
            correlation_detector=self.correlation_detector,
            portfolio_risk=self.portfolio_risk,
        )

    def _check_circuit_breaker(self, failed):
        return risk_checks.check_circuit_breaker(self.circuit_breaker, failed)

    def _check_liquidity(self, market, proposed_cost, failed, warnings):
        return risk_checks.check_liquidity(market, proposed_cost, failed, warnings)

    def _check_existing_position(self, signal, failed, warnings):
        return risk_checks.check_existing_position(
            self.settings, self.positions, signal, failed, warnings,
        )

    def _check_signal_quality(self, signal, proposed_cost, failed):
        return risk_checks.check_signal_quality(self.settings, signal, proposed_cost, failed)

    def _check_resolution_date(self, market, failed, warnings):
        return risk_checks.check_resolution_date(market, failed, warnings)

    def _check_cooldown(self, market_id, failed):
        return risk_checks.check_cooldown(
            market_id, self._cooldowns, self._cooldown_durations,
            self.cooldown_seconds, failed, db=self.db,
        )

    def _check_wash_trade(self, market_id, failed):
        return risk_checks.check_wash_trade(market_id, self.db, failed)

    def _check_manipulation(self, market, failed):
        return risk_checks.check_manipulation(self.manipulation_detector, market, failed)

    def _check_strategy_exposure(self, signal, bankroll, proposed_cost, failed):
        return risk_checks.check_strategy_exposure(
            self.settings, self.positions, signal, bankroll, proposed_cost, failed,
        )

    def _check_obvious_no_limit(self, signal, bankroll, proposed_cost, failed):
        return risk_checks.check_obvious_no_limit(
            self.settings, self.positions, signal, bankroll, proposed_cost, failed,
        )

    def _check_max_concurrent_positions(self, signal, failed):
        return risk_checks.check_max_concurrent_positions(
            self.settings, self.positions, signal, failed,
        )

    def _check_spread_vs_edge(self, signal, market, failed):
        return risk_checks.check_spread_vs_edge(signal, market, failed)

    def _get_min_edge(self, strategy):
        return risk_checks.get_min_edge(self.settings, strategy)
