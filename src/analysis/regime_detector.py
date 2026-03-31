"""Market Regime Detection — adaptive thresholds based on volatility environment.

Analyzes recent market snapshots across all tracked markets to classify
the current regime (low-vol, normal, high-vol, crisis). Each regime has
multipliers for edge thresholds and Kelly sizing:

- LOW_VOL: Markets calm → relax edge requirements, increase sizing
- NORMAL: Default parameters
- HIGH_VOL: Elevated volatility → tighten edge, reduce sizing
- CRISIS: Extreme moves → very conservative (election weeks, Fed shocks)

This prevents overtrading during chaos and increases aggression in calm periods.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class MarketRegime(Enum):
    """Market regime classification."""

    LOW_VOL = "low_vol"
    NORMAL = "normal"
    HIGH_VOL = "high_vol"
    CRISIS = "crisis"


@dataclass(frozen=True)
class RegimeMultipliers:
    """Multipliers applied to trading parameters based on regime."""

    edge_multiplier: float   # Applied to min_edge — higher = stricter
    kelly_multiplier: float  # Applied to Kelly fraction — lower = smaller positions

    @property
    def description(self) -> str:
        return (
            f"edge×{self.edge_multiplier:.1f}, "
            f"kelly×{self.kelly_multiplier:.1f}"
        )


# Regime → multiplier mapping
REGIME_MULTIPLIERS: dict[MarketRegime, RegimeMultipliers] = {
    MarketRegime.LOW_VOL: RegimeMultipliers(edge_multiplier=0.8, kelly_multiplier=1.2),
    MarketRegime.NORMAL: RegimeMultipliers(edge_multiplier=1.0, kelly_multiplier=1.0),
    MarketRegime.HIGH_VOL: RegimeMultipliers(edge_multiplier=1.5, kelly_multiplier=0.6),
    MarketRegime.CRISIS: RegimeMultipliers(edge_multiplier=2.0, kelly_multiplier=0.3),
}


@dataclass
class RegimeAnalysis:
    """Result of regime detection analysis."""

    regime: MarketRegime
    multipliers: RegimeMultipliers
    cross_market_volatility: float  # Std dev of 24h price changes
    volume_spike_ratio: float       # Current volume vs 7-day average
    markets_analyzed: int
    detail: str = ""


class RegimeDetector:
    """Detects market regime from recent snapshot data.

    Analyzes cross-market volatility (std dev of 24h price changes across
    all tracked markets) and volume spikes (current vs 7-day average) to
    classify the current environment.
    """

    # Volatility thresholds (std dev of 24h absolute price changes)
    VOL_LOW = 0.02       # < 2% avg price change = calm
    VOL_HIGH = 0.06      # > 6% avg price change = elevated
    VOL_CRISIS = 0.12    # > 12% avg price change = crisis

    # Volume spike thresholds (ratio of recent volume to 7-day average)
    VOLUME_SPIKE_HIGH = 2.0    # 2x normal volume
    VOLUME_SPIKE_CRISIS = 4.0  # 4x normal volume

    # Minimum markets needed for meaningful regime detection
    MIN_MARKETS = 5

    def detect_regime(self, db) -> RegimeAnalysis:
        """Analyze recent snapshots to determine current market regime.

        Args:
            db: Database instance with get_snapshots_for_market()

        Returns:
            RegimeAnalysis with regime classification and multipliers
        """
        now = datetime.now(timezone.utc)
        lookback_24h = (now - timedelta(hours=24)).isoformat()
        lookback_7d = (now - timedelta(days=7)).isoformat()

        # Get all market IDs that have recent snapshots
        try:
            conn = db._get_conn()
            market_ids = [
                row[0] for row in conn.execute(
                    "SELECT DISTINCT market_id FROM market_snapshots "
                    "WHERE timestamp >= ? LIMIT 200",
                    (lookback_7d,),
                ).fetchall()
            ]
        except Exception as e:
            logger.warning(f"Regime detection: failed to query markets: {e}")
            return self._default_analysis("DB query failed")

        if len(market_ids) < self.MIN_MARKETS:
            return self._default_analysis(
                f"Only {len(market_ids)} markets with snapshots (need {self.MIN_MARKETS})"
            )

        # Compute per-market 24h price changes and volume metrics
        price_changes: list[float] = []
        recent_volumes: list[float] = []
        avg_volumes_7d: list[float] = []

        for market_id in market_ids:
            try:
                snapshots_24h = db.get_snapshots_for_market(
                    market_id, start=lookback_24h,
                )
                snapshots_7d = db.get_snapshots_for_market(
                    market_id, start=lookback_7d,
                )

                if len(snapshots_24h) >= 2:
                    first_price = snapshots_24h[0]["yes_price"]
                    last_price = snapshots_24h[-1]["yes_price"]
                    if first_price > 0:
                        change = abs(last_price - first_price)
                        price_changes.append(change)

                # Volume: sum of recent vs average
                if snapshots_24h:
                    vol_24h = sum(s.get("volume_1h", 0) or 0 for s in snapshots_24h)
                    recent_volumes.append(vol_24h)

                if snapshots_7d:
                    vol_7d = sum(s.get("volume_1h", 0) or 0 for s in snapshots_7d)
                    days_of_data = max(1, len(snapshots_7d) / max(1, len(snapshots_24h)))
                    avg_daily = vol_7d / max(days_of_data, 1)
                    avg_volumes_7d.append(avg_daily)

            except Exception as e:
                logger.debug(f"Regime: skipping {market_id}: {e}")
                continue

        if len(price_changes) < self.MIN_MARKETS:
            return self._default_analysis(
                f"Only {len(price_changes)} markets with price data"
            )

        # Cross-market volatility: mean absolute 24h price change.
        # Using mean (not std dev) because uniform large moves across all
        # markets is high volatility, even if dispersion is low.
        cross_vol = sum(price_changes) / len(price_changes)

        # Volume spike: median of (recent / 7d average) ratios
        volume_ratio = 1.0
        if recent_volumes and avg_volumes_7d:
            ratios = []
            for recent, avg in zip(recent_volumes, avg_volumes_7d):
                if avg > 0:
                    ratios.append(recent / avg)
            if ratios:
                ratios.sort()
                volume_ratio = ratios[len(ratios) // 2]  # median

        # Classify regime
        regime = self._classify(cross_vol, volume_ratio)

        detail = (
            f"vol={cross_vol:.4f} (thresholds: low<{self.VOL_LOW}, "
            f"high>{self.VOL_HIGH}, crisis>{self.VOL_CRISIS}), "
            f"volume_ratio={volume_ratio:.1f}x"
        )

        logger.info(
            f"Regime detected: {regime.value} — {detail} "
            f"({len(price_changes)} markets analyzed)"
        )

        return RegimeAnalysis(
            regime=regime,
            multipliers=REGIME_MULTIPLIERS[regime],
            cross_market_volatility=cross_vol,
            volume_spike_ratio=volume_ratio,
            markets_analyzed=len(price_changes),
            detail=detail,
        )

    def _classify(self, volatility: float, volume_ratio: float) -> MarketRegime:
        """Classify regime from volatility and volume metrics.

        Uses a two-axis approach: volatility is primary, volume spikes
        can escalate the regime by one level.
        """
        # Primary classification from volatility
        if volatility >= self.VOL_CRISIS:
            return MarketRegime.CRISIS
        elif volatility >= self.VOL_HIGH:
            # High vol + volume spike → crisis
            if volume_ratio >= self.VOLUME_SPIKE_CRISIS:
                return MarketRegime.CRISIS
            return MarketRegime.HIGH_VOL
        elif volatility >= self.VOL_LOW:
            # Normal vol + volume spike → high vol
            if volume_ratio >= self.VOLUME_SPIKE_HIGH:
                return MarketRegime.HIGH_VOL
            return MarketRegime.NORMAL
        else:
            # Low vol + volume spike → normal (volume alone doesn't mean calm)
            if volume_ratio >= self.VOLUME_SPIKE_HIGH:
                return MarketRegime.NORMAL
            return MarketRegime.LOW_VOL

    @staticmethod
    def _std_dev(values: list[float]) -> float:
        """Compute population standard deviation."""
        if not values:
            return 0.0
        n = len(values)
        mean = sum(values) / n
        variance = sum((x - mean) ** 2 for x in values) / n
        return math.sqrt(variance)

    @staticmethod
    def _default_analysis(reason: str) -> RegimeAnalysis:
        """Return NORMAL regime when detection can't run."""
        logger.debug(f"Regime detection defaulting to NORMAL: {reason}")
        return RegimeAnalysis(
            regime=MarketRegime.NORMAL,
            multipliers=REGIME_MULTIPLIERS[MarketRegime.NORMAL],
            cross_market_volatility=0.0,
            volume_spike_ratio=1.0,
            markets_analyzed=0,
            detail=f"default — {reason}",
        )
