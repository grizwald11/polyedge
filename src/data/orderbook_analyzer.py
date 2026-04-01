"""Order book imbalance analyzer — supplementary confidence signal.

Calculates volume-weighted midpoint and buy/sell imbalance from order book
data. Exposes these as confidence modifiers for the AI probability strategy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class OrderBookAnalysis:
    """Result of analyzing an order book."""
    vwap_bid: float          # Volume-weighted average bid price
    vwap_ask: float          # Volume-weighted average ask price
    vwap_midpoint: float     # Midpoint between VWAP bid and ask
    bid_depth: float         # Total quantity on bid side
    ask_depth: float         # Total quantity on ask side
    imbalance_ratio: float   # bid_depth / ask_depth (>1 = buy pressure)
    has_buy_imbalance: bool  # bid_depth > 2x ask_depth
    has_sell_imbalance: bool # ask_depth > 2x bid_depth
    confidence_modifier: float  # -0.15 to +0.15 adjustment to edge confidence
    spread: float            # Best ask - best bid
    levels_bid: int          # Number of price levels on bid side
    levels_ask: int          # Number of price levels on ask side


# Imbalance thresholds
STRONG_IMBALANCE = 2.0    # >2x ratio = strong signal
MODERATE_IMBALANCE = 1.5  # >1.5x ratio = moderate signal
MAX_CONFIDENCE_MOD = 0.15  # Maximum confidence modifier
MIN_DEPTH_FOR_SIGNAL = 50  # Minimum total depth (contracts) to trust imbalance


def analyze_orderbook(
    orderbook: dict,
    side: str = "yes",
) -> Optional[OrderBookAnalysis]:
    """Analyze an order book and return imbalance metrics.

    Args:
        orderbook: Dict with "yes" and "no" keys, each containing
            [[price, quantity], ...] levels. Kalshi format.
        side: Which side to analyze ("yes" or "no").

    Returns:
        OrderBookAnalysis or None if insufficient data.
    """
    if not orderbook:
        return None

    bids_raw = orderbook.get(side, [])
    asks_raw = orderbook.get("no" if side == "yes" else "yes", [])

    if not bids_raw and not asks_raw:
        return None

    bids = _parse_levels(bids_raw)
    asks = _parse_levels(asks_raw)

    if not bids and not asks:
        return None

    # Volume-weighted average prices
    vwap_bid = _vwap(bids) if bids else 0.0
    vwap_ask = _vwap(asks) if asks else 1.0

    # For binary markets: bid side wants YES, ask side is selling YES (wants NO)
    # Convert NO prices to YES-equivalent for comparison
    # NO price of X means YES ask of (1-X)
    if side == "yes" and asks:
        asks = [(1.0 - p, q) for p, q in asks]
        vwap_ask = 1.0 - _vwap([(p, q) for p, q in orderbook.get("no", []) if _safe_float(p) > 0]) if asks else 1.0

    bid_depth = sum(q for _, q in bids)
    ask_depth = sum(q for _, q in asks)
    total_depth = bid_depth + ask_depth

    if total_depth == 0:
        return None

    # Imbalance ratio
    if ask_depth > 0:
        imbalance_ratio = bid_depth / ask_depth
    else:
        imbalance_ratio = float("inf") if bid_depth > 0 else 1.0

    # Spread
    best_bid = max((p for p, _ in bids), default=0.0)
    best_ask = min((p for p, _ in asks), default=1.0)
    spread = max(0.0, best_ask - best_bid)

    # VWAP midpoint
    if vwap_bid > 0 and vwap_ask > 0:
        vwap_midpoint = (vwap_bid + vwap_ask) / 2
    elif vwap_bid > 0:
        vwap_midpoint = vwap_bid
    else:
        vwap_midpoint = vwap_ask

    # Imbalance detection
    has_buy = imbalance_ratio >= STRONG_IMBALANCE and total_depth >= MIN_DEPTH_FOR_SIGNAL
    has_sell = (1.0 / imbalance_ratio if imbalance_ratio > 0 else float("inf")) >= STRONG_IMBALANCE and total_depth >= MIN_DEPTH_FOR_SIGNAL

    # Confidence modifier: boost when book supports our side, reduce when against
    confidence_modifier = _compute_confidence_modifier(
        imbalance_ratio, total_depth
    )

    return OrderBookAnalysis(
        vwap_bid=round(vwap_bid, 4),
        vwap_ask=round(vwap_ask, 4),
        vwap_midpoint=round(vwap_midpoint, 4),
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        imbalance_ratio=round(imbalance_ratio, 2),
        has_buy_imbalance=has_buy,
        has_sell_imbalance=has_sell,
        confidence_modifier=round(confidence_modifier, 4),
        spread=round(spread, 4),
        levels_bid=len(bids),
        levels_ask=len(asks),
    )


def apply_orderbook_signal(
    edge: float,
    confidence: float,
    analysis: Optional[OrderBookAnalysis],
    direction: str,
) -> tuple[float, float]:
    """Apply orderbook imbalance as a supplementary confidence signal.

    Args:
        edge: Current calculated edge (e.g., 0.08)
        confidence: Current confidence (0-1)
        analysis: OrderBookAnalysis or None
        direction: "BUY_YES" or "BUY_NO"

    Returns:
        (adjusted_edge, adjusted_confidence)
    """
    if analysis is None:
        return edge, confidence

    modifier = analysis.confidence_modifier

    # If buying YES and there's buy imbalance, that's supportive
    # If buying YES and there's sell imbalance, that's adverse
    if direction == "BUY_NO":
        modifier = -modifier  # Flip for NO side

    adjusted_confidence = max(0.0, min(1.0, confidence + modifier))

    # Edge adjustment: scale edge by confidence modifier direction
    # Strong book support → up to 10% edge boost
    # Strong book opposition → up to 10% edge reduction
    edge_scale = 1.0 + (modifier * 0.5)  # ±7.5% edge scaling at max
    adjusted_edge = edge * max(0.5, min(1.5, edge_scale))

    if abs(modifier) > 0.05:
        logger.info(
            f"Orderbook signal: imbalance={analysis.imbalance_ratio:.1f}x, "
            f"modifier={modifier:+.2f}, confidence {confidence:.2f}→{adjusted_confidence:.2f}, "
            f"edge {edge:.3f}→{adjusted_edge:.3f}"
        )

    return adjusted_edge, adjusted_confidence


def _parse_levels(levels: list) -> list[tuple[float, float]]:
    """Parse orderbook levels to (price, quantity) tuples."""
    parsed = []
    for level in levels:
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            p = _safe_float(level[0])
            q = _safe_float(level[1])
            if p > 0 and q > 0:
                parsed.append((p, q))
    return parsed


def _safe_float(v) -> float:
    """Safely convert to float."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _vwap(levels: list[tuple[float, float]]) -> float:
    """Volume-weighted average price."""
    if not levels:
        return 0.0
    total_value = sum(p * q for p, q in levels)
    total_quantity = sum(q for _, q in levels)
    if total_quantity == 0:
        return 0.0
    return total_value / total_quantity


def _compute_confidence_modifier(
    imbalance_ratio: float,
    total_depth: float,
) -> float:
    """Compute confidence modifier from imbalance ratio.

    Returns value between -MAX_CONFIDENCE_MOD and +MAX_CONFIDENCE_MOD.
    Positive = buy pressure (bid-heavy), negative = sell pressure (ask-heavy).
    """
    if total_depth < MIN_DEPTH_FOR_SIGNAL:
        return 0.0

    # Normalize ratio to [-1, 1] scale
    # ratio > 1 → positive (buy pressure), ratio < 1 → negative (sell pressure)
    if imbalance_ratio >= 1.0:
        # Log scale for buy pressure: 1x=0, 2x=0.5, 4x=1.0
        import math
        raw = min(1.0, math.log2(imbalance_ratio))
    else:
        # Inverse for sell pressure: 0.5x=-0.5, 0.25x=-1.0
        import math
        raw = -min(1.0, math.log2(1.0 / max(imbalance_ratio, 0.01)))

    # Scale by depth confidence: more depth = more trustworthy signal
    depth_confidence = min(1.0, total_depth / 500)  # Full confidence at 500+ contracts

    return raw * MAX_CONFIDENCE_MOD * depth_confidence
