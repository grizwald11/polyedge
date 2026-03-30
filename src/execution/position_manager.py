"""Position manager — tracks open positions, P&L, and exposure.

Aggregates trades into positions and provides portfolio-level metrics.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.core.models import (
    Direction, Market, Order, OrderStatus, Position, Side, StrategyName, Trade,
)
from src.storage.database import Database

# Exit thresholds — all configurable via PositionManager constructor kwargs.
# Rationale for each default is documented inline.
DEFAULT_STOP_LOSS_PCT = 0.30
# 30%: Binary markets can recover from drawdowns, but a 30% loss on cost basis
# indicates the thesis is likely wrong. Prevents runaway losses while allowing
# normal price volatility.

DEFAULT_MAX_HOLD_DAYS = 21
# 21 days: Frees capital faster than holding to near-expiry. Most of the edge
# in a correctly-assessed market is captured in the first few weeks; holding
# longer ties up capital that could be redeployed into fresh opportunities.

DEFAULT_EDGE_GONE_THRESHOLD = 0.20
# 20%: Exit when less than 20% of the original estimated edge remains. At this
# point the expected return no longer justifies continued capital allocation.

DEFAULT_TRAILING_STOP_ACTIVATE = 0.12
# 12%: Activate trailing stop only after locking in at least a 12% gain.
# Below this threshold, normal price noise would trigger too many premature exits.

DEFAULT_TRAILING_STOP_DISTANCE = 0.50
# 50%: Trail 50% of peak gain (e.g., peak +30% → exit at +15%). Captures most
# of the upside while protecting against sharp reversals near market resolution.

DEFAULT_TAKE_PROFIT_PCT = 0.80
# 80%: Take profit when 80% of the maximum theoretical gain is realized
# (e.g., bought YES at $0.60, take profit at ~$0.92). Avoids diminishing returns
# of holding to $0.99 while significantly reducing late-stage resolution risk.

DEFAULT_CAPITAL_ROTATION_EDGE = 0.40
# 40%: When total exposure exceeds 35%, exit positions where less than 40% of
# original edge remains. Prioritizes deploying capital into higher-edge
# opportunities over squeezing the last few percent from nearly-exhausted positions.

# Slippage buffer: exits trigger slightly before the hard threshold
# to account for execution slippage (typically 1-3%) — H-13
SLIPPAGE_BUFFER = 0.02
# 2%: Ensures exit orders actually execute at or before the hard threshold after
# typical market spread and order routing latency.

if __name__ != "__main__":
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from src.core.kalshi_client import KalshiClient

logger = logging.getLogger(__name__)


class PositionManager:
    """Tracks all open positions and portfolio metrics."""

    def __init__(
        self,
        db: Database,
        bankroll: float = 500.0,
        stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
        max_hold_days: float = DEFAULT_MAX_HOLD_DAYS,
        edge_gone_threshold: float = DEFAULT_EDGE_GONE_THRESHOLD,
        trailing_stop_activate: float = DEFAULT_TRAILING_STOP_ACTIVATE,
        trailing_stop_distance: float = DEFAULT_TRAILING_STOP_DISTANCE,
        take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
        capital_rotation_edge: float = DEFAULT_CAPITAL_ROTATION_EDGE,
    ):
        self.db = db
        self.bankroll = bankroll
        # Exit thresholds — configurable via settings.execution.*
        self._stop_loss_pct = stop_loss_pct
        self._max_hold_days = max_hold_days
        self._edge_gone_threshold = edge_gone_threshold
        self._trailing_stop_activate = trailing_stop_activate
        self._trailing_stop_distance = trailing_stop_distance
        self._take_profit_pct = take_profit_pct
        self._capital_rotation_edge = capital_rotation_edge
        self._positions: dict[str, Position] = {}  # market_id -> Position
        self._pending_exits: set[str] = set()  # market_ids with resting exit orders
        self._load_positions_from_db()

    def update_from_trade(self, trade: Trade, market_question: str = "") -> Position:
        """Update positions based on a new trade fill.

        Args:
            trade: The completed trade
            market_question: Human-readable market question

        Returns:
            Updated position
        """
        key = trade.market_id
        existing = self._positions.get(key)

        if existing is None:
            # New position
            direction = self._direction_from_trade(trade)
            position = Position(
                market_id=trade.market_id,
                market_question=market_question,
                token_id=trade.token_id,
                direction=direction,
                size=trade.size,
                avg_entry_price=trade.price,
                current_price=trade.price,
                unrealized_pnl=0.0,
                total_fees=trade.fee,
                buy_fees=trade.fee,
                strategy=trade.strategy,
                paper=trade.paper,
                opened_at=trade.timestamp,
                last_updated=trade.timestamp,
            )
            self._positions[key] = position
            logger.info(
                f"Opened position: {direction.value} {trade.size:.0f}x "
                f"{trade.market_id} @ ${trade.price:.2f}"
            )
            return position
        else:
            # Update existing position
            if trade.side == Side.BUY:
                # Adding to position — weighted average entry
                total_cost = existing.avg_entry_price * existing.size + trade.price * trade.size
                existing.size += trade.size
                existing.avg_entry_price = round(total_cost / existing.size, 6) if existing.size > 0 else 0
                # Accumulate fees on buy — round to prevent float drift
                existing.total_fees = round(existing.total_fees + trade.fee, 4)
                existing.buy_fees = round(existing.buy_fees + trade.fee, 4)
            else:
                # Reducing position — avg_entry_price stays the same
                # (it represents the cost basis of remaining contracts)
                sell_size = trade.size
                if sell_size > existing.size:
                    logger.warning(
                        f"Sell size ({sell_size}) exceeds position size ({existing.size}) "
                        f"for {trade.market_id} — clamping to position size"
                    )
                    sell_size = existing.size
                    trade.size = sell_size  # Persist clamped size for DB consistency

                # Proportional buy fee for the contracts being sold
                proportional_buy_fee = (
                    existing.buy_fees * (sell_size / existing.size)
                    if existing.size > 0 else 0.0
                )

                # Calculate realized P&L: gross profit minus both buy and sell fees
                realized_pnl = (
                    (trade.price - existing.avg_entry_price) * sell_size
                    - proportional_buy_fee
                    - trade.fee
                )
                trade.realized_pnl = round(realized_pnl, 4)

                # Update the DB record with the calculated P&L
                self._update_trade_pnl(trade)

                logger.info(
                    f"Realized P&L on {trade.market_id}: ${realized_pnl:+.2f} "
                    f"(sold {sell_size:.0f}x @ ${trade.price:.2f}, "
                    f"entry @ ${existing.avg_entry_price:.2f})"
                )

                # Reduce buy_fees proportionally (remaining fees stay with remaining contracts)
                existing.buy_fees = round(existing.buy_fees - proportional_buy_fee, 4)
                # Accumulate sell fee into total_fees for record-keeping
                existing.total_fees = round(existing.total_fees + trade.fee, 4)
                existing.size -= sell_size
                if existing.size <= 0:
                    # Position closed
                    self._positions.pop(key, None)
                    logger.info(f"Closed position: {trade.market_id}")
                    return existing

            existing.last_updated = trade.timestamp
            logger.info(
                f"Updated position: {existing.direction.value} {existing.size:.0f}x "
                f"{trade.market_id} @ avg ${existing.avg_entry_price:.2f}"
            )
            return existing

    def update_price(self, market_id: str, yes_price: float, no_price: float = 0.0):
        """Update current price and recalculate unrealized P&L.

        Args:
            market_id: Market ticker
            yes_price: Current YES price
            no_price: Current NO price (used for BUY_NO/SELL_NO positions)
        """
        position = self._positions.get(market_id)
        if position is None:
            return

        # Skip invalid prices — 0.0 means no data available
        if yes_price <= 0 and no_price <= 0:
            return

        # Stale price detection: warn if price hasn't changed in a while.
        # This catches dead data feeds that silently replay old prices.
        now = datetime.now(timezone.utc)
        time_since_update = (now - position.last_updated).total_seconds()
        price_for_side = no_price if position.direction in (Direction.BUY_NO, Direction.SELL_NO) else yes_price
        if time_since_update > 300 and position.current_price > 0:
            price_delta = abs(price_for_side - position.current_price)
            if price_delta == 0:
                position._price_stale = True
                logger.warning(
                    f"Stale price detected for {market_id}: ${price_for_side:.2f} unchanged "
                    f"for {time_since_update:.0f}s — data feed may be dead"
                )
            elif price_delta < 0.005:
                position._price_stale = True
                logger.warning(
                    f"Potentially stale price for {market_id}: moved only ${price_delta:.4f} "
                    f"in {time_since_update:.0f}s — data feed may be replaying old prices"
                )
            else:
                position._price_stale = False
        else:
            position._price_stale = False

        # Use the price matching the position's side, but only if valid
        if position.direction in (Direction.BUY_NO, Direction.SELL_NO):
            if no_price <= 0:
                return  # No valid price for this position's side
            position.current_price = no_price
        else:
            if yes_price <= 0:
                return  # No valid price for this position's side
            position.current_price = yes_price
        # P&L = (current - entry) * size for BUY, (entry - current) * size for SELL
        # Round to 4dp to prevent floating-point drift in accumulated P&L
        if position.direction in (Direction.BUY_YES, Direction.BUY_NO):
            position.unrealized_pnl = round((position.current_price - position.avg_entry_price) * position.size, 4)
        else:
            position.unrealized_pnl = round((position.avg_entry_price - position.current_price) * position.size, 4)
        # Track peak P&L for trailing stop
        if position.unrealized_pnl > position.peak_pnl:
            position.peak_pnl = position.unrealized_pnl
        position.last_updated = datetime.now(timezone.utc)

    def get_position(self, market_id: str) -> Optional[Position]:
        """Get position for a specific market."""
        return self._positions.get(market_id)

    def get_all_positions(self) -> list[Position]:
        """Get all open positions."""
        return list(self._positions.values())

    def get_total_exposure(self) -> float:
        """Total capital deployed across all positions."""
        return round(sum(p.cost_basis for p in self._positions.values()), 4)

    def get_total_exposure_pct(self) -> float:
        """Total exposure as percentage of bankroll."""
        if self.bankroll <= 0:
            return 0.0
        return self.get_total_exposure() / self.bankroll

    def get_total_unrealized_pnl(self) -> float:
        """Sum of unrealized P&L across all positions."""
        return round(sum(p.unrealized_pnl for p in self._positions.values()), 4)

    def get_strategy_exposure(self, strategy: StrategyName) -> float:
        """Total exposure for a specific strategy."""
        return round(sum(
            p.cost_basis for p in self._positions.values()
            if p.strategy == strategy
        ), 4)

    def has_position(self, market_id: str) -> bool:
        """Check if we already have a position in this market."""
        return market_id in self._positions

    def mark_pending_exit(self, market_id: str) -> None:
        """Mark a position as having a resting exit order in flight."""
        self._pending_exits.add(market_id)

    def clear_pending_exit(self, market_id: str) -> None:
        """Clear pending exit flag (order filled or cancelled)."""
        self._pending_exits.discard(market_id)

    def has_pending_exit(self, market_id: str) -> bool:
        """Check if a position has a resting exit order already submitted."""
        return market_id in self._pending_exits

    def get_position_count(self) -> int:
        """Number of open positions."""
        return len(self._positions)

    def should_exit(
        self,
        position: Position,
        market: Market | None = None,
        stop_loss_pct: float | None = None,
        max_hold_days: float | None = None,
        edge_gone_threshold: float | None = None,
    ) -> tuple[bool, str]:
        """Determine if a position should be exited.

        Checks five conditions:
        1. Stop-loss: unrealized loss exceeds threshold of cost basis
        2. Trailing stop: gain dropped significantly from peak
        3. Take-profit: captured most of max theoretical gain
        4. Time-based: position held longer than max_hold_days
        5. Edge-gone: market price moved past our entry (edge evaporated)

        Args:
            position: The position to evaluate
            market: Current market data (needed for edge-gone check)
            stop_loss_pct: Override instance threshold (defaults to self._stop_loss_pct)
            max_hold_days: Override instance threshold (defaults to self._max_hold_days)
            edge_gone_threshold: Override instance threshold (defaults to self._edge_gone_threshold)

        Returns:
            (should_exit, reason) tuple
        """
        # Use instance thresholds unless caller overrides
        stop_loss_pct = stop_loss_pct if stop_loss_pct is not None else self._stop_loss_pct
        max_hold_days = max_hold_days if max_hold_days is not None else self._max_hold_days
        edge_gone_threshold = edge_gone_threshold if edge_gone_threshold is not None else self._edge_gone_threshold

        cost_basis = position.cost_basis
        if cost_basis <= 0:
            return False, ""

        # 1. Stop-loss check
        #    Require fresh price data (<2 min) to avoid false exits on stale prices.
        #    Trigger slightly before threshold to account for slippage (H-13).
        effective_stop_loss = stop_loss_pct - SLIPPAGE_BUFFER
        if position.unrealized_pnl < 0:
            loss_pct = abs(position.unrealized_pnl) / cost_basis
            if loss_pct >= effective_stop_loss:
                price_age = (datetime.now(timezone.utc) - position.last_updated).total_seconds()
                if price_age > 120:
                    logger.warning(
                        f"Stop-loss blocked by stale price for {position.market_id}: "
                        f"loss={loss_pct:.0%}, price age={price_age:.0f}s"
                    )
                else:
                    return True, f"stop_loss: {loss_pct:.0%} loss exceeds {effective_stop_loss:.0%} threshold (incl. {SLIPPAGE_BUFFER:.0%} slippage buffer)"

        # 2. Trailing stop: if we've had a significant gain and it's pulling back.
        #    Apply SLIPPAGE_BUFFER: activate slightly later (higher threshold) to
        #    avoid triggering on momentary price dips that fill above the stop.
        #    Require fresh price data (<2 min) for trailing stop to avoid false exits.
        effective_trailing_activate = self._trailing_stop_activate + SLIPPAGE_BUFFER
        if position.peak_pnl > 0 and cost_basis > 0:
            peak_gain_pct = position.peak_pnl / cost_basis
            if peak_gain_pct >= effective_trailing_activate:
                trail_floor = position.peak_pnl * self._trailing_stop_distance
                if position.unrealized_pnl < trail_floor:
                    price_age = (datetime.now(timezone.utc) - position.last_updated).total_seconds()
                    if price_age > 120:
                        logger.debug(
                            f"Skipping trailing_stop for {position.market_id}: "
                            f"price data stale ({price_age:.0f}s old)"
                        )
                    else:
                        return True, (
                            f"trailing_stop: current P&L ${position.unrealized_pnl:.2f} "
                            f"dropped below trail floor ${trail_floor:.2f} "
                            f"(peak ${position.peak_pnl:.2f}, "
                            f"activate threshold {effective_trailing_activate:.0%} incl. {SLIPPAGE_BUFFER:.0%} slippage buffer)"
                        )

        # 3. Take-profit: capture gains when near max theoretical payout.
        #    Apply SLIPPAGE_BUFFER: trigger slightly earlier (lower threshold) to
        #    ensure the limit order fills near the target rather than overshooting.
        # BUY_YES/BUY_NO: max gain = (1.0 - entry) * size (payout is $1.00)
        # SELL_YES/SELL_NO: max gain = entry * size (payout is $0.00)
        effective_take_profit = self._take_profit_pct - SLIPPAGE_BUFFER
        if position.direction in (Direction.BUY_YES, Direction.BUY_NO):
            max_gain = (1.0 - position.avg_entry_price) * position.size
        else:
            max_gain = position.avg_entry_price * position.size
        if max_gain > 0 and position.unrealized_pnl >= max_gain * effective_take_profit:
            return True, (
                f"take_profit: captured {position.unrealized_pnl / max_gain:.0%} of max gain "
                f"(${position.unrealized_pnl:.2f} / ${max_gain:.2f}, "
                f"threshold {effective_take_profit:.0%} incl. {SLIPPAGE_BUFFER:.0%} slippage buffer)"
            )

        # 4. Time-based exit
        now = datetime.now(timezone.utc)
        hold_time = now - position.opened_at
        if hold_time.total_seconds() / 86400 > max_hold_days:
            return True, f"time_exit: held {hold_time.days} days (max {max_hold_days:.0f})"

        # Also exit if market is closing soon and we're underwater
        if market and market.end_date:
            days_left = market.days_to_resolution
            if days_left is not None and days_left < 1 and position.unrealized_pnl < 0:
                return True, f"expiry_exit: market closes in {days_left:.1f} days, position underwater"

        # 5. Edge-gone check.
        #    Apply SLIPPAGE_BUFFER: use a slightly higher threshold so we exit
        #    before the edge fully evaporates, accounting for execution slippage.
        effective_edge_gone = edge_gone_threshold + SLIPPAGE_BUFFER
        if market is not None:
            remaining_edge = self._calculate_remaining_edge(position, market)
            if remaining_edge < effective_edge_gone:
                return True, (
                    f"edge_gone: remaining edge {remaining_edge:.1%} < "
                    f"{effective_edge_gone:.1%} threshold (incl. {SLIPPAGE_BUFFER:.0%} slippage buffer)"
                )

        # 6. Capital rotation: when portfolio is crowded, exit profitable positions
        #    where most of the edge has been captured to free capital for new trades.
        #    Skip if market price data is stale (>5 min) to avoid exiting on outdated edge calc.
        if market is not None and position.unrealized_pnl > 0:
            price_age = (datetime.now(timezone.utc) - position.last_updated).total_seconds()
            if price_age > 300:
                logger.debug(
                    f"Skipping capital_rotation for {position.market_id}: "
                    f"price data stale ({price_age:.0f}s old)"
                )
            else:
                exposure_pct = self.get_total_exposure_pct()
                remaining = self._calculate_remaining_edge(position, market)
                if exposure_pct > 0.35 and remaining < self._capital_rotation_edge:
                    return True, (
                        f"capital_rotation: edge {remaining:.1%} < {self._capital_rotation_edge:.0%} "
                        f"threshold with portfolio at {exposure_pct:.0%} exposure"
                    )

        return False, ""

    def get_exit_candidates(
        self,
        markets: dict[str, Market] | None = None,
    ) -> list[tuple[Position, str]]:
        """Scan all positions and return those that should be exited.

        Args:
            markets: Market lookup {ticker: Market} for edge-gone checks

        Returns:
            List of (position, reason) tuples
        """
        candidates: list[tuple[Position, str]] = []
        for market_id, position in self._positions.items():
            market = markets.get(market_id) if markets else None
            should, reason = self.should_exit(position, market)
            if should:
                candidates.append((position, reason))
                logger.info(f"Exit candidate: {market_id} — {reason}")
        return candidates

    def _calculate_remaining_edge(self, position: Position, market: Market) -> float:
        """Calculate remaining edge for a position given current market prices.

        BUY positions profit when the bought token's price rises toward 1.0.
        SELL positions profit when the sold token's price drops toward 0.0.

        Returns a fraction in [0, 1]:
        - 1.0 = all original upside remains (price hasn't moved from entry)
        - 0.5 = half the upside captured
        - 0.0 = price moved against us (underwater) or fully captured
        """
        entry = position.avg_entry_price
        if entry <= 0:
            return 0.0

        if position.direction == Direction.BUY_YES:
            # Bought YES at entry, profits as yes_price → 1.0
            current = market.yes_price
            if current <= 0:
                return 0.0
            if current < entry:
                return 0.0  # underwater
            original_upside = 1.0 - entry
            if original_upside <= 0:
                return 0.0
            return max(0.0, min(1.0, (1.0 - current) / original_upside))

        elif position.direction == Direction.BUY_NO:
            # Bought NO at entry, profits as no_price → 1.0
            current = market.no_price
            if current <= 0:
                return 0.0
            if current < entry:
                return 0.0  # underwater
            original_upside = 1.0 - entry
            if original_upside <= 0:
                return 0.0
            return max(0.0, min(1.0, (1.0 - current) / original_upside))

        elif position.direction == Direction.SELL_YES:
            # Sold YES at entry, profits as yes_price → 0.0
            current = market.yes_price
            if current <= 0:
                return 0.0
            if current > entry:
                return 0.0  # underwater — price rose above our sell
            original_upside = entry  # max gain = entry (price drops to 0)
            if original_upside <= 0:
                return 0.0
            return max(0.0, min(1.0, current / original_upside))

        else:  # SELL_NO
            # Sold NO at entry, profits as no_price → 0.0
            current = market.no_price
            if current <= 0:
                return 0.0
            if current > entry:
                return 0.0  # underwater — price rose above our sell
            original_upside = entry  # max gain = entry (price drops to 0)
            if original_upside <= 0:
                return 0.0
            return max(0.0, min(1.0, current / original_upside))

    async def sync_with_kalshi(self, kalshi, auto_correct: bool = True) -> int:
        """Reconcile local positions against Kalshi API positions.

        When auto_correct is True, adjusts local state to match Kalshi:
        - Adds positions found on Kalshi but missing locally
        - Removes local live positions not found on Kalshi

        Args:
            kalshi: KalshiClient instance
            auto_correct: If True, auto-correct mismatches. If False, only log.

        Returns:
            Count of mismatches found (and corrected if auto_correct=True)
        """
        try:
            api_positions = await kalshi.get_positions()
        except Exception as e:
            logger.error(f"Failed to sync positions with Kalshi: {e}", exc_info=True)
            return 0

        if not api_positions:
            return 0

        mismatches = 0
        api_tickers = set()

        for api_pos in api_positions:
            ticker = api_pos.get("ticker", "")
            if not ticker:
                continue
            api_tickers.add(ticker)

            # Kalshi returns market_exposure (dollar value) or position counts
            api_exposure = api_pos.get("market_exposure", 0)
            local_pos = self.get_position(ticker)

            if local_pos is None and api_exposure != 0:
                mismatches += 1
                if auto_correct:
                    # Reconstruct a position from API data
                    yes_count = api_pos.get("yes_count", 0) or 0
                    no_count = api_pos.get("no_count", 0) or 0
                    if yes_count > 0:
                        direction = Direction.BUY_YES
                        size = yes_count
                        token_id = f"{ticker}_yes"
                    elif no_count > 0:
                        direction = Direction.BUY_NO
                        size = no_count
                        token_id = f"{ticker}_no"
                    else:
                        logger.warning(
                            f"Position mismatch: Kalshi has exposure in {ticker} "
                            f"but no yes/no count — skipping auto-correct"
                        )
                        continue

                    avg_price = api_pos.get("average_price", 50) / 100.0  # cents to dollars
                    position = Position(
                        market_id=ticker,
                        market_question=api_pos.get("title", ticker),
                        token_id=token_id,
                        direction=direction,
                        size=float(size),
                        avg_entry_price=avg_price,
                        current_price=avg_price,
                        unrealized_pnl=0.0,
                        strategy=StrategyName.AI_PROBABILITY,  # Unknown — default
                        paper=False,
                        opened_at=datetime.now(timezone.utc),
                        last_updated=datetime.now(timezone.utc),
                    )
                    self._positions[ticker] = position
                    logger.warning(
                        f"Position AUTO-CORRECTED: added {direction.value} {size}x "
                        f"{ticker} @ ${avg_price:.2f} from Kalshi API"
                    )
                else:
                    logger.warning(
                        f"Position mismatch: Kalshi has position in {ticker}, "
                        f"local tracker does not"
                    )

        # Check for local positions not on Kalshi
        stale_keys = []
        for market_id, local_pos in self._positions.items():
            if not local_pos.paper and market_id not in api_tickers:
                mismatches += 1
                if auto_correct:
                    stale_keys.append(market_id)
                    logger.warning(
                        f"Position AUTO-CORRECTED: removed stale live position "
                        f"in {market_id} (not found on Kalshi)"
                    )
                else:
                    logger.warning(
                        f"Position mismatch: local tracker has live position "
                        f"in {market_id}, Kalshi does not"
                    )

        for key in stale_keys:
            self._positions.pop(key, None)

        if mismatches:
            action = "corrected" if auto_correct else "found"
            logger.warning(f"Position sync: {mismatches} mismatches {action}")
        else:
            logger.debug(f"Position sync OK: {len(api_tickers)} Kalshi positions checked")

        return mismatches

    def _load_positions_from_db(self):
        """Reconstruct open positions from trade history on startup.

        Replays all trades chronologically. After reconstruction, validates
        positions for consistency (H-11): warns on negative size or sell trades
        that exceeded accumulated BUY size at the time of the trade.
        """
        conn = self.db._get_conn()
        rows = conn.execute(
            "SELECT order_id, market_id, token_id, side, price, size, "
            "fee, realized_pnl, strategy, paper, timestamp "
            "FROM trades ORDER BY timestamp ASC"
        ).fetchall()

        if not rows:
            return

        # Track running BUY totals per market to validate SELL trades at replay time
        running_buy_totals: dict[str, float] = {}

        for row in rows:
            try:
                trade = Trade(
                    order_id=row["order_id"],
                    market_id=row["market_id"],
                    token_id=row["token_id"],
                    side=Side(row["side"]),
                    price=row["price"],
                    size=row["size"],
                    fee=row["fee"],
                    realized_pnl=row["realized_pnl"],
                    strategy=StrategyName(row["strategy"]),
                    paper=bool(row["paper"]),
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                )

                market_id = trade.market_id
                if trade.side == Side.BUY:
                    running_buy_totals[market_id] = (
                        running_buy_totals.get(market_id, 0.0) + trade.size
                    )
                elif trade.side == Side.SELL:
                    accumulated = running_buy_totals.get(market_id, 0.0)
                    if trade.size > accumulated + 1e-9:
                        logger.warning(
                            f"DB consistency: SELL size {trade.size:.0f} exceeds "
                            f"accumulated BUY {accumulated:.0f} for {market_id} "
                            f"(order_id={trade.order_id}) — clamping to {accumulated:.0f}"
                        )
                        trade.size = max(0.0, accumulated)
                    running_buy_totals[market_id] = max(
                        0.0, running_buy_totals.get(market_id, 0.0) - trade.size
                    )

                if trade.size < 0:
                    logger.warning(
                        f"DB consistency: negative size {trade.size} for "
                        f"{market_id} (order_id={trade.order_id}) — skipping"
                    )
                    continue

                self.update_from_trade(trade)
            except (ValueError, KeyError, TypeError) as e:
                logger.warning(
                    f"Skipping corrupted trade row (order_id={row.get('order_id', '?')}): {e}"
                )

        # Post-load validation: warn on any positions with negative size
        for market_id, pos in list(self._positions.items()):
            if pos.size < 0:
                logger.warning(
                    f"DB consistency: position {market_id} has negative size "
                    f"{pos.size:.4f} after replay — removing invalid position"
                )
                self._positions.pop(market_id, None)

        if self._positions:
            logger.info(f"Loaded {len(self._positions)} open positions from DB")

    def _update_trade_pnl(self, trade: Trade):
        """Update the realized_pnl in the DB for a trade that was already persisted."""
        try:
            conn = self.db._get_conn()
            conn.execute(
                "UPDATE trades SET realized_pnl = ? WHERE order_id = ?",
                (trade.realized_pnl, trade.order_id),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to update trade P&L in DB: {e}", exc_info=True)

    def _direction_from_trade(self, trade: Trade) -> Direction:
        """Infer direction from trade side and token."""
        is_yes = "yes" in trade.token_id.lower()
        if trade.side == Side.BUY:
            return Direction.BUY_YES if is_yes else Direction.BUY_NO
        else:
            return Direction.SELL_YES if is_yes else Direction.SELL_NO
