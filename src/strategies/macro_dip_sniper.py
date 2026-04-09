"""Macro Dip Sniper strategy — buy cheap OTM threshold markets in euphoric regimes.

Inspired by an anonymous Polymarket trader documented by @kirillk_web3 who turned
783 bets into ~$1.046M in 3 months using "macro dip sniping": zero bots, zero
arbitrage, no scalping. The recipe:

- During euphoric / low-vol regimes, scan price-threshold markets ("Will BTC dip
  to $X?", "Will ETH fall below $Y?")
- Buy YES on out-of-the-money dips trading at 10-15¢
- Hold to resolution
- Asymmetric payoff: +570% to +798% ROI on individual plays

The edge: in calm bull regimes, downside threshold markets get severely
underpriced because nobody believes a dip can happen, but realised vol means
transient dips are far more frequent than the implied 10-15% probability.

This strategy filters the market universe to threshold markets, gates entry on a
LOW_VOL regime, and uses the existing Claude forecaster to estimate the true
probability. Sizing flows through the standard half-Kelly sizer with a stricter
per-position cap (1.5%) and a cap of 8 concurrent positions to manage variance.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional

from src.config import Settings
from src.core.models import Direction, Market, MarketCategory, Signal, StrategyName

if TYPE_CHECKING:
    from src.analysis.claude_forecaster import ClaudeForecaster
    from src.analysis.regime_detector import RegimeAnalysis

logger = logging.getLogger(__name__)

# Strategy parameters — kept as module constants to mirror the mean_reversion
# strategy style. Only min_edge is pulled from settings.
MAX_YES_PRICE = 0.15             # Cap on YES entry price — matches the tweet's "10-15¢"
MIN_YES_PRICE = 0.03             # Floor — avoid overlap with obvious_no territory
MIN_DAYS_TO_RESOLUTION = 3.0     # Too soon = pinned, not enough time for a dip
MAX_DAYS_TO_RESOLUTION = 30.0    # Too far = high uncertainty, low IRR
MAX_POSITION_PCT = 0.015         # 1.5% cap — high variance, tighter than the 5% global cap
MAX_CONCURRENT_POSITIONS = 8     # Cap on simultaneous dip-sniper positions
MAX_CANDIDATES_PER_CYCLE = 5     # Bound Claude API spend per cycle
MIN_VOLUME_24H = 5_000           # Avoid dead markets where we can't exit if needed

# Keywords that identify "threshold / dip" style questions. Any of these in the
# question text makes the market a candidate. We also require a number/price
# token to appear somewhere in the question (see _THRESHOLD_NUMBER_PATTERN).
_THRESHOLD_KEYWORDS = (
    "dip",
    "fall",
    "falls",
    "drop",
    "drops",
    "below",
    "under",
    "crash",
    "plunge",
    "sink",
    "tumble",
    "slump",
    "reach",  # "will BTC reach $X" is symmetric — will match but the price filter handles it
    "hit",
)

# Matches dollar amounts and numeric thresholds in question text, including
# common abbreviations like "$58k", "$2,500", "5000", "3.5%", "2.5M". Used
# only to confirm the question contains a quantitative threshold — we don't
# need the extracted value for sizing.
_THRESHOLD_NUMBER_PATTERN = re.compile(
    r"\$\s*\d+(?:[.,]\d+)*\s*[kKmMbB]?"   # $58k, $2,500, $100
    r"|\b\d+(?:\.\d+)?\s*[kKmMbB]\b"       # 58k, 2.5M (no dollar sign)
    r"|\b\d{1,3}(?:,\d{3})+\b"             # 2,500 (comma-separated thousands)
    r"|\b\d+(?:\.\d+)?\s*%"                # 3.5%, 50%
    r"|\b\d{4,}\b"                         # 5000, 10000 (4+ bare digits)
)

# Categories where asymmetric threshold markets are plausible. CRYPTO is
# included here even though it's weight=0 in categories.yaml because the macro
# dip sniper carries its own universe filter (the overall scanner may still
# pass crypto markets through when other consumers ignore them).
_ALLOWED_CATEGORIES = frozenset({
    MarketCategory.CRYPTO,
    MarketCategory.FED_MACRO,
    MarketCategory.GEOPOLITICS,
    MarketCategory.EARNINGS,
    MarketCategory.OTHER,
})

# Per obvious_no.py, Kalshi charges ~1.75% per contract on CRYPTO and SPORTS.
# We subtract the fee from our edge to avoid taking trades that are only
# profitable gross-of-fees.
_FEE_CATEGORIES = frozenset({MarketCategory.CRYPTO, MarketCategory.SPORTS})
_FEE_RATE = 0.0175


class MacroDipSniperStrategy:
    """Buy cheap OTM price-threshold markets during euphoric regimes."""

    def __init__(
        self,
        settings: Settings,
        forecaster: "ClaudeForecaster",
    ):
        self.settings = settings
        self.forecaster = forecaster
        self._active_entries: set[str] = set()  # market_id tracker (best-effort)

    async def generate_signals(
        self,
        markets: list[Market],
        regime_analysis: Optional["RegimeAnalysis"] = None,
    ) -> list[Signal]:
        """Scan markets for macro dip sniping opportunities.

        Args:
            markets: Pre-filtered active markets from the scanner.
            regime_analysis: Current regime analysis. The strategy only fires
                in LOW_VOL regimes — if not provided or not LOW_VOL, no signals
                are generated.

        Returns:
            List of BUY_YES signals on OTM threshold markets.
        """
        # Regime gate — only hunt in calm / euphoric regimes
        if not self._is_low_vol_regime(regime_analysis):
            logger.debug(
                "Macro dip sniper: regime gate closed "
                f"(regime={getattr(regime_analysis, 'regime', 'unknown')})"
            )
            return []

        # Concurrency cap
        if len(self._active_entries) >= MAX_CONCURRENT_POSITIONS:
            logger.debug(
                f"Macro dip sniper: at max concurrent positions "
                f"({MAX_CONCURRENT_POSITIONS})"
            )
            return []

        # Filter to candidate threshold markets
        candidates = [m for m in markets if self._is_candidate(m)]
        if not candidates:
            logger.debug("Macro dip sniper: no candidate threshold markets this cycle")
            return []

        # Cap Claude API spend per cycle. Prefer cheaper entries first since
        # they have the highest asymmetric payoff.
        candidates.sort(key=lambda m: m.yes_price)
        candidates = candidates[:MAX_CANDIDATES_PER_CYCLE]

        min_edge = self.settings.trading.min_edge_macro_dip_sniper
        signals: list[Signal] = []

        for market in candidates:
            signal = await self._assess_candidate(market, min_edge)
            if signal is not None:
                signals.append(signal)

        logger.info(
            f"Macro dip sniper: scanned {len(markets)} markets, "
            f"{len(candidates)} candidates, {len(signals)} signals"
        )
        return signals

    def mark_position_opened(self, market_id: str) -> None:
        """Called by the executor when a signal from this strategy fills."""
        self._active_entries.add(market_id)

    def mark_position_closed(self, market_id: str) -> None:
        """Called by the executor/position manager when a position closes."""
        self._active_entries.discard(market_id)

    # ──────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────

    @staticmethod
    def _is_low_vol_regime(regime_analysis: Optional["RegimeAnalysis"]) -> bool:
        """True only if the current regime is LOW_VOL."""
        if regime_analysis is None:
            return False
        # Imported lazily to keep module import cheap and avoid a hard
        # dependency cycle during test collection.
        from src.analysis.regime_detector import MarketRegime
        return regime_analysis.regime == MarketRegime.LOW_VOL

    def _is_candidate(self, market: Market) -> bool:
        """Check structural filters: category, question pattern, price, time, volume."""
        # Category filter
        category = market.category
        if category not in _ALLOWED_CATEGORIES:
            return False

        # Must be a binary market with two tokens
        if not market.is_binary:
            return False

        # Question must look like a threshold/dip question
        if not self._is_threshold_question(market.question):
            return False

        # Price filter
        yes_price = market.yes_price
        if not (MIN_YES_PRICE <= yes_price <= MAX_YES_PRICE):
            return False

        # Time-to-resolution filter
        days = market.days_to_resolution
        if days is None:
            return False
        if not (MIN_DAYS_TO_RESOLUTION <= days <= MAX_DAYS_TO_RESOLUTION):
            return False

        # Liquidity floor — avoid markets where we can't realistically exit
        if market.volume_24h < MIN_VOLUME_24H:
            return False

        return True

    @staticmethod
    def _is_threshold_question(question: str) -> bool:
        """True if the question text contains a threshold keyword AND a numeric target."""
        if not question:
            return False
        q_lower = question.lower()
        has_keyword = any(kw in q_lower for kw in _THRESHOLD_KEYWORDS)
        if not has_keyword:
            return False
        has_number = bool(_THRESHOLD_NUMBER_PATTERN.search(question))
        return has_number

    async def _assess_candidate(
        self, market: Market, min_edge: float,
    ) -> Optional[Signal]:
        """Call Claude forecaster on a candidate and build a signal if edge is sufficient."""
        try:
            forecast = await self.forecaster.assess_market(market)
        except Exception as e:
            # Defensive — forecaster normally handles its own errors and
            # returns a ForecastResult with parse_failed=True, but we don't
            # want a surprise exception to bring down the whole scan cycle.
            logger.warning(
                f"Macro dip sniper: forecaster threw for {market.ticker}: {e}"
            )
            return None

        if forecast is None or getattr(forecast, "parse_failed", False):
            return None

        p_est = float(forecast.probability)
        yes_price = market.yes_price

        # Raw edge on the YES side: our estimate minus the market's implied prob
        raw_edge = p_est - yes_price

        # Fee adjustment for categories that charge per-contract fees
        fee = self._per_contract_fee(market, yes_price)
        net_edge = raw_edge - fee

        if net_edge < min_edge:
            return None

        # Confidence grows with edge but is capped — this is a high-variance
        # strategy and we don't want Kelly to over-size a single dip bet.
        confidence = min(0.70, max(0.40, 0.40 + net_edge))

        reasoning = (
            f"OTM dip @ YES={yes_price:.2f}, p_est={p_est:.2f}, "
            f"raw_edge={raw_edge:.2f}, fee={fee:.4f}, net_edge={net_edge:.2f}, "
            f"regime=LOW_VOL. {forecast.reasoning[:120] if forecast.reasoning else ''}"
        )

        signal = Signal(
            strategy=StrategyName.MACRO_DIP_SNIPER,
            market_id=market.ticker,
            market_question=market.question,
            direction=Direction.BUY_YES,
            edge=net_edge,
            probability_estimate=p_est,
            market_price=yes_price,
            confidence=confidence,
            reasoning=reasoning.strip(),
        )

        logger.info(
            f"Macro dip sniper: '{market.question[:60]}...' "
            f"YES=${yes_price:.2f}, p_est={p_est:.2f}, edge={net_edge:.2f}"
        )
        return signal

    @staticmethod
    def _per_contract_fee(market: Market, yes_price: float) -> float:
        """Fee per contract for fee-enabled Kalshi categories (crypto, sports)."""
        category = getattr(market, "category", None)
        if category not in _FEE_CATEGORIES:
            return 0.0
        # Mirrors the obvious_no fee calculation — Kalshi's 1.75% fee is
        # applied to the implied variance of the contract (price * (1-price)).
        return round(_FEE_RATE * yes_price * (1.0 - yes_price), 6)
