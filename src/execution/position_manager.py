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

# Exit thresholds
DEFAULT_STOP_LOSS_PCT = 0.50       # Exit if unrealized loss > 50% of cost basis
DEFAULT_MAX_HOLD_DAYS = 30         # Exit if held > 30 days
DEFAULT_EDGE_GONE_THRESHOLD = 0.01 # Exit if remaining edge < 1%

if __name__ != "__main__":
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from src.core.kalshi_client import KalshiClient

logger = logging.getLogger(__name__)


class PositionManager:
    """Tracks all open positions and portfolio metrics."""

    def __init__(self, db: Database, bankroll: float = 500.0):
        self.db = db
        self.bankroll = bankroll
        self._positions: dict[str, Position] = {}  # market_id -> Position
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
                existing.avg_entry_price = total_cost / existing.size if existing.size > 0 else 0
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

        # Use the price matching the position's side
        if position.direction in (Direction.BUY_NO, Direction.SELL_NO):
            position.current_price = no_price
        else:
            position.current_price = yes_price
        # P&L = (current - entry) * size for BUY, (entry - current) * size for SELL
        if position.direction in (Direction.BUY_YES, Direction.BUY_NO):
            position.unrealized_pnl = (position.current_price - position.avg_entry_price) * position.size
        else:
            position.unrealized_pnl = (position.avg_entry_price - position.current_price) * position.size
        position.last_updated = datetime.now(timezone.utc)

    def get_position(self, market_id: str) -> Optional[Position]:
        """Get position for a specific market."""
        return self._positions.get(market_id)

    def get_all_positions(self) -> list[Position]:
        """Get all open positions."""
        return list(self._positions.values())

    def get_total_exposure(self) -> float:
        """Total capital deployed across all positions."""
        return sum(p.cost_basis for p in self._positions.values())

    def get_total_exposure_pct(self) -> float:
        """Total exposure as percentage of bankroll."""
        if self.bankroll <= 0:
            return 0.0
        return self.get_total_exposure() / self.bankroll

    def get_total_unrealized_pnl(self) -> float:
        """Sum of unrealized P&L across all positions."""
        return sum(p.unrealized_pnl for p in self._positions.values())

    def get_strategy_exposure(self, strategy: StrategyName) -> float:
        """Total exposure for a specific strategy."""
        return sum(
            p.cost_basis for p in self._positions.values()
            if p.strategy == strategy
        )

    def has_position(self, market_id: str) -> bool:
        """Check if we already have a position in this market."""
        return market_id in self._positions

    def get_position_count(self) -> int:
        """Number of open positions."""
        return len(self._positions)

    def should_exit(
        self,
        position: Position,
        market: Market | None = None,
        stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
        max_hold_days: float = DEFAULT_MAX_HOLD_DAYS,
        edge_gone_threshold: float = DEFAULT_EDGE_GONE_THRESHOLD,
    ) -> tuple[bool, str]:
        """Determine if a position should be exited.

        Checks three conditions:
        1. Stop-loss: unrealized loss exceeds threshold of cost basis
        2. Time-based: position held longer than max_hold_days
        3. Edge-gone: market price moved past our entry (edge evaporated)

        Args:
            position: The position to evaluate
            market: Current market data (needed for edge-gone check)
            stop_loss_pct: Max loss as fraction of cost basis before exit
            max_hold_days: Max days to hold before time-based exit
            edge_gone_threshold: Min remaining edge to justify holding

        Returns:
            (should_exit, reason) tuple
        """
        cost_basis = position.cost_basis
        if cost_basis <= 0:
            return False, ""

        # 1. Stop-loss check
        if position.unrealized_pnl < 0:
            loss_pct = abs(position.unrealized_pnl) / cost_basis
            if loss_pct >= stop_loss_pct:
                return True, f"stop_loss: {loss_pct:.0%} loss exceeds {stop_loss_pct:.0%} threshold"

        # 2. Time-based exit
        now = datetime.now(timezone.utc)
        hold_time = now - position.opened_at
        if hold_time.total_seconds() / 86400 > max_hold_days:
            return True, f"time_exit: held {hold_time.days} days (max {max_hold_days:.0f})"

        # Also exit if market is closing soon and we're underwater
        if market and market.end_date:
            days_left = market.days_to_resolution
            if days_left is not None and days_left < 1 and position.unrealized_pnl < 0:
                return True, f"expiry_exit: market closes in {days_left:.1f} days, position underwater"

        # 3. Edge-gone check
        if market is not None:
            remaining_edge = self._calculate_remaining_edge(position, market)
            if remaining_edge < edge_gone_threshold:
                return True, f"edge_gone: remaining edge {remaining_edge:.1%} < {edge_gone_threshold:.1%}"

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

        Edge is measured as the difference between where we think the market
        should resolve and the current price. For a BUY_YES position, our entry
        price implies we believed the true probability was >= entry price.
        If the market price has moved toward 1.0 (our thesis), edge shrinks
        because there's less upside. If price moved away, we still have edge
        but are underwater.

        Returns a positive fraction if the position still has favorable risk/reward,
        zero or negative if the edge has evaporated.
        """
        if position.direction in (Direction.BUY_YES, Direction.SELL_NO):
            # We're long YES — we entered expecting the price to rise toward 1.0.
            # Remaining edge = how much room there is between current price and 1.0,
            # relative to what existed when we entered.
            # If price moved UP past entry, edge is consumed (less upside left).
            # If price moved DOWN below entry, edge expanded but we're losing.
            current = market.yes_price
            entry = position.avg_entry_price
            if current <= 0 or entry <= 0:
                return 0.0
            # Edge at entry: (1 - entry) / entry
            # Edge now: (1 - current) / current
            # Remaining = current edge as fraction of entry edge
            entry_edge = (1.0 - entry) / entry
            current_edge = (1.0 - current) / current
            if entry_edge <= 0:
                return 0.0
            return max(0.0, current_edge / entry_edge - 0.5)  # <50% of original edge = exit
        else:
            # We're long NO
            current = market.no_price
            entry = position.avg_entry_price
            if current <= 0 or entry <= 0:
                return 0.0
            entry_edge = (1.0 - entry) / entry
            current_edge = (1.0 - current) / current
            if entry_edge <= 0:
                return 0.0
            return max(0.0, current_edge / entry_edge - 0.5)

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
            logger.error(f"Failed to sync positions with Kalshi: {e}")
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
        """Reconstruct open positions from trade history on startup."""
        conn = self.db._get_conn()
        rows = conn.execute(
            "SELECT order_id, market_id, token_id, side, price, size, "
            "fee, realized_pnl, strategy, paper, timestamp "
            "FROM trades ORDER BY timestamp ASC"
        ).fetchall()

        if not rows:
            return

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
                self.update_from_trade(trade)
            except (ValueError, KeyError, TypeError) as e:
                logger.warning(
                    f"Skipping corrupted trade row (order_id={row.get('order_id', '?')}): {e}"
                )

        if self._positions:
            logger.info(f"Loaded {len(self._positions)} open positions from DB")

    def _direction_from_trade(self, trade: Trade) -> Direction:
        """Infer direction from trade side and token."""
        is_yes = "yes" in trade.token_id.lower()
        if trade.side == Side.BUY:
            return Direction.BUY_YES if is_yes else Direction.BUY_NO
        else:
            return Direction.SELL_YES if is_yes else Direction.SELL_NO
