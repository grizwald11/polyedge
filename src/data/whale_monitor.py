"""Whale monitor — tracks positions of proven profitable traders.

Monitors a curated basket of whale wallets/usernames, detects new
position entries, and generates consensus signals.

Note: Kalshi API doesn't expose other users' positions publicly.
This module works with a manually curated basket loaded from YAML config.
Position tracking relies on market-level data and curated intelligence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from src.config import Settings
from src.core.models import Direction, WhaleSignal, WhaleWallet
from src.storage.database import Database

logger = logging.getLogger(__name__)


@dataclass
class WhalePosition:
    """A whale's position in a market."""
    wallet: str
    market_id: str
    direction: Direction
    size: float = 0.0
    entry_price: float = 0.0
    detected_at: datetime = None

    def __post_init__(self):
        if self.detected_at is None:
            self.detected_at = datetime.now(timezone.utc)


class WhaleMonitor:
    """Monitors whale wallet basket and detects consensus signals."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._basket: list[WhaleWallet] = []
        self._positions: dict[str, dict[str, WhalePosition]] = {}  # wallet -> {market_id -> pos}
        self._max_tracked_markets_per_wallet = 200  # L-4: Prevent unbounded growth
        self._load_basket()

    def _load_basket(self):
        """Load whale basket from YAML config."""
        basket_path = Path(self.settings.whales.basket_path)
        if not basket_path.exists():
            logger.info(f"Whale basket not found at {basket_path}")
            return

        try:
            with open(basket_path) as f:
                data = yaml.safe_load(f) or {}

            wallets = data.get("wallets", [])
            for w in wallets:
                whale = WhaleWallet(
                    address=w.get("address", w.get("username", "")),
                    alias=w.get("alias", ""),
                    win_rate=w.get("win_rate", 0.0),
                    total_pnl=w.get("total_pnl", 0.0),
                    total_trades=w.get("total_trades", 0),
                    categories=w.get("categories", []),
                )
                self._basket.append(whale)

            logger.info(f"Loaded {len(self._basket)} whale wallets from basket")
        except Exception as e:
            logger.error(f"Failed to load whale basket: {e}", exc_info=True)

    @property
    def basket_size(self) -> int:
        return len(self._basket)

    def update_positions(
        self, wallet: str, positions: dict[str, WhalePosition]
    ):
        """Update tracked positions for a wallet.

        Args:
            wallet: Wallet address/username
            positions: Current positions {market_id: WhalePosition}
        """
        previous = self._positions.get(wallet, {})
        self._positions[wallet] = positions

        # Detect new entries
        new_markets = set(positions.keys()) - set(previous.keys())
        for market_id in new_markets:
            pos = positions[market_id]
            logger.info(
                f"Whale {wallet[:8]}... entered {market_id}: "
                f"{pos.direction.value} @ ${pos.entry_price:.2f}"
            )
            self._log_whale_trade(wallet, pos)

    def get_consensus(self, market_id: str) -> Optional[WhaleSignal]:
        """Check if whale basket has consensus on a market.

        Returns WhaleSignal if ≥ threshold of basket agrees on direction.
        """
        if not self._basket:
            return None

        yes_count = 0
        no_count = 0
        wallets_yes: list[str] = []
        wallets_no: list[str] = []
        earliest_entry: Optional[datetime] = None
        prices_yes: list[float] = []
        prices_no: list[float] = []

        for wallet in self._basket:
            positions = self._positions.get(wallet.address, {})
            pos = positions.get(market_id)
            if pos is None:
                continue

            if pos.direction in (Direction.BUY_YES, Direction.SELL_NO):
                yes_count += 1
                wallets_yes.append(wallet.address)
                if pos.entry_price > 0:
                    prices_yes.append(pos.entry_price)
            else:
                no_count += 1
                wallets_no.append(wallet.address)
                if pos.entry_price > 0:
                    prices_no.append(pos.entry_price)
            if pos.detected_at:
                if earliest_entry is None or pos.detected_at < earliest_entry:
                    earliest_entry = pos.detected_at

        total = yes_count + no_count
        if total == 0 or len(self._basket) == 0:
            return None

        # Require at least 3 positioned whales to avoid thin consensus (e.g. 1/1)
        MIN_POSITIONED_WHALES = 3
        if total < MIN_POSITIONED_WHALES:
            logger.debug(
                "Whale consensus: only %d/%d whales positioned on %s (need %d)",
                total, len(self._basket), market_id, MIN_POSITIONED_WHALES,
            )
            return None

        threshold = self.settings.whales.consensus_threshold

        # Consensus is among positioned whales, not entire basket.
        # A whale with no position on this market hasn't "voted" either way.
        yes_pct = yes_count / total
        no_pct = no_count / total

        if yes_pct >= threshold:
            return WhaleSignal(
                market_id=market_id,
                direction=Direction.BUY_YES,
                whale_count=yes_count,
                basket_size=len(self._basket),
                consensus_pct=yes_pct,
                avg_entry_price=sum(prices_yes) / len(prices_yes) if prices_yes else 0.0,
                earliest_entry=earliest_entry,
                wallets=wallets_yes,
            )
        elif no_pct >= threshold:
            return WhaleSignal(
                market_id=market_id,
                direction=Direction.BUY_NO,
                whale_count=no_count,
                basket_size=len(self._basket),
                consensus_pct=no_pct,
                avg_entry_price=sum(prices_no) / len(prices_no) if prices_no else 0.0,
                earliest_entry=earliest_entry,
                wallets=wallets_no,
            )

        return None

    def get_all_markets_with_positions(self) -> set[str]:
        """Get all market_ids where any whale has a position."""
        markets: set[str] = set()
        for wallet_positions in self._positions.values():
            markets.update(wallet_positions.keys())
        return markets

    def _log_whale_trade(self, wallet: str, pos: WhalePosition):
        """Log a whale trade to the database.

        L-1 fix: Uses Database.log_whale_trade() abstraction layer.
        """
        self.db.log_whale_trade(
            wallet_address=wallet,
            market_id=pos.market_id,
            direction=pos.direction.value,
            size=pos.size,
            price=pos.entry_price,
            detected_at=pos.detected_at.isoformat(),
        )
