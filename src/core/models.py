"""Core data models — Pydantic models used throughout the system.

All data flowing through PolyEdge is typed through these models.
Models are designed for Phase 1 but include fields needed by later phases.

Kalshi prices are in cents (1-99). We store prices in dollars (0.01-0.99)
internally for consistency. Use cents_to_dollars() and dollars_to_cents()
for conversion at the API boundary. Polymarket prices are already in
dollars (0.0-1.0) and need no conversion.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ──────────────────────────────────────────────
# Price Conversion Helpers
# ──────────────────────────────────────────────

def cents_to_dollars(cents: int | float) -> float:
    """Convert Kalshi cents (1-99) to dollars (0.01-0.99)."""
    return float(cents) / 100.0


def dollars_to_cents(dollars: float) -> int:
    """Convert dollars (0.01-0.99) to Kalshi cents (1-99)."""
    return int(round(dollars * 100))


# ──────────────────────────────────────────────
# Fee Calculation Helpers
# ──────────────────────────────────────────────

def kalshi_taker_fee(contracts: int, price_cents: int) -> int:
    """Calculate Kalshi taker fee in cents. Formula: ceil(0.07 * contracts * price * (1-price))."""
    p = price_cents / 100.0
    return math.ceil(0.07 * contracts * p * (1 - p))


def kalshi_maker_fee(contracts: int, price_cents: int) -> int:
    """Calculate Kalshi maker fee in cents. Formula: ceil(0.0175 * contracts * price * (1-price))."""
    p = price_cents / 100.0
    return math.ceil(0.0175 * contracts * p * (1 - p))


def polymarket_fee(contracts: int, price: float) -> float:
    """Polymarket event markets are fee-free for maker orders."""
    return 0.0


# ──────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────

class Platform(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Direction(str, Enum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"
    SELL_YES = "SELL_YES"
    SELL_NO = "SELL_NO"


class OrderType(str, Enum):
    GTC = "GTC"   # Good-til-cancelled (maker/limit)
    FOK = "FOK"   # Fill-or-kill (taker/market)
    GTD = "GTD"   # Good-til-date


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class StrategyName(str, Enum):
    AI_PROBABILITY = "ai_probability"
    CROSS_ARB = "cross_arb"
    WHALE_TRACKER = "whale_tracker"
    NEWS_REACTIVE = "news_reactive"
    OBVIOUS_NO = "obvious_no"
    CROSS_PLATFORM_ARB = "cross_platform_arb"


class MarketCategory(str, Enum):
    POLITICS = "Politics"
    GEOPOLITICS = "Geopolitics"
    FED_MACRO = "Fed/Macro"
    TECH_AI = "Tech/AI"
    CULTURE = "Culture"
    CRYPTO = "Crypto"
    SPORTS = "Sports"
    EARNINGS = "Earnings"
    OTHER = "Other"


class LiquidityTier(str, Enum):
    HIGH = "high"       # >$50K depth
    MEDIUM = "medium"   # $10K-$50K depth
    LOW = "low"         # <$10K depth


class TokenOutcome(str, Enum):
    YES = "Yes"
    NO = "No"


# ──────────────────────────────────────────────
# Market Models
# ──────────────────────────────────────────────

class MarketToken(BaseModel):
    """A single outcome token (YES or NO) within a market."""
    token_id: str
    outcome: TokenOutcome
    price: float = 0.0  # In dollars (0.01-0.99)
    winner: Optional[bool] = None


class Market(BaseModel):
    """A single prediction market (Kalshi or Polymarket)."""
    ticker: str  # Kalshi ticker or Polymarket condition_id
    platform: Platform = Platform.KALSHI
    question: str
    description: str = ""
    category: MarketCategory = MarketCategory.OTHER
    tags: list[str] = Field(default_factory=list)
    tokens: list[MarketToken] = Field(default_factory=list)
    end_date: Optional[datetime] = None
    volume_24h: float = 0.0
    volume_total: float = 0.0
    liquidity: float = 0.0
    spread: float = 0.0
    active: bool = True
    closed: bool = False
    resolution_source: str = ""
    slug: str = ""
    subtitle: str = ""
    event_ticker: str = ""
    result: str = ""
    status: str = ""

    # Convenience properties
    @property
    def yes_token(self) -> Optional[MarketToken]:
        for t in self.tokens:
            if t.outcome == TokenOutcome.YES:
                return t
        return None

    @property
    def no_token(self) -> Optional[MarketToken]:
        for t in self.tokens:
            if t.outcome == TokenOutcome.NO:
                return t
        return None

    @property
    def yes_price(self) -> float:
        t = self.yes_token
        return t.price if t else 0.0

    @property
    def no_price(self) -> float:
        t = self.no_token
        return t.price if t else 0.0

    @property
    def implied_probability(self) -> float:
        return self.yes_price

    @property
    def days_to_resolution(self) -> Optional[float]:
        if not self.end_date:
            return None
        now = datetime.now(timezone.utc)
        end = self.end_date
        # Make both timezone-aware for comparison
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        delta = end - now
        return max(0, delta.total_seconds() / 86400)

    @property
    def is_binary(self) -> bool:
        return len(self.tokens) == 2


class MarketSnapshot(BaseModel):
    """A point-in-time snapshot of market prices."""
    market_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    yes_price: float
    no_price: float
    spread: float
    volume_1h: float = 0.0
    liquidity: float = 0.0


# ──────────────────────────────────────────────
# Signal Models
# ──────────────────────────────────────────────

class Signal(BaseModel):
    """A trading signal generated by a strategy."""
    id: Optional[str] = None
    strategy: StrategyName
    market_id: str
    platform: Platform = Platform.KALSHI
    market_question: str = ""
    direction: Direction
    edge: float  # Our probability - market probability
    probability_estimate: float  # Our estimated true probability
    market_price: float  # Current market price
    confidence: float = 0.5  # 0-1 confidence in the estimate
    reasoning: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    acted_on: bool = False
    order_id: Optional[str] = None

    @field_validator("edge")
    @classmethod
    def edge_finite(cls, v: float) -> float:
        import math
        if not math.isfinite(v):
            raise ValueError(f"edge must be finite, got {v}")
        if v < -1.0 or v > 1.0:
            raise ValueError(f"edge must be in [-1, 1], got {v}")
        return v

    @field_validator("probability_estimate")
    @classmethod
    def probability_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"probability_estimate must be in [0, 1], got {v}")
        return v

    @field_validator("market_price")
    @classmethod
    def market_price_in_range(cls, v: float) -> float:
        if not (0.0 < v < 1.0):
            raise ValueError(f"market_price must be in (0, 1), got {v}")
        return v

    @field_validator("confidence")
    @classmethod
    def confidence_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {v}")
        return v


# ──────────────────────────────────────────────
# Order & Trade Models
# ──────────────────────────────────────────────

class Order(BaseModel):
    """An order placed on Kalshi or Polymarket."""
    id: Optional[str] = None
    market_id: str
    platform: Platform = Platform.KALSHI
    token_id: str
    side: Side
    price: float
    size: float  # Number of contracts
    cost: float = 0.0  # price * size
    order_type: OrderType = OrderType.GTC
    fee_rate_bps: int = 0
    status: OrderStatus = OrderStatus.PENDING
    strategy: StrategyName = StrategyName.AI_PROBABILITY
    signal_id: Optional[str] = None
    paper: bool = True  # Paper trade or live
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    filled_at: Optional[datetime] = None
    fill_price: Optional[float] = None
    cancelled_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    @field_validator("price")
    @classmethod
    def price_valid(cls, v: float) -> float:
        if v <= 0 or v > 0.99:
            raise ValueError(f"price must be in (0, 0.99], got {v}")
        return v

    @field_validator("size")
    @classmethod
    def size_positive(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"size must be >= 0, got {v}")
        return v


class Position(BaseModel):
    """An open position (aggregated from fills)."""
    market_id: str
    platform: Platform = Platform.KALSHI
    market_question: str = ""
    token_id: str
    direction: Direction
    size: float  # Contracts held
    avg_entry_price: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    total_fees: float = 0.0  # Accumulated fees paid for this position (buy + sell)
    buy_fees: float = 0.0    # Buy-side fees only (for proportional P&L deduction on sell)
    strategy: StrategyName = StrategyName.AI_PROBABILITY
    paper: bool = True
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    peak_pnl: float = 0.0  # Highest unrealized P&L seen (for trailing stop)

    @property
    def market_value(self) -> float:
        return self.size * self.current_price

    @property
    def cost_basis(self) -> float:
        return self.size * self.avg_entry_price + self.buy_fees


class Trade(BaseModel):
    """A completed trade (fill)."""
    id: Optional[str] = None
    order_id: str
    market_id: str
    platform: Platform = Platform.KALSHI
    token_id: str
    side: Side
    price: float
    size: float
    fee: float = 0.0
    realized_pnl: float = 0.0
    strategy: StrategyName = StrategyName.AI_PROBABILITY
    paper: bool = True
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ──────────────────────────────────────────────
# Calibration Models
# ──────────────────────────────────────────────

class CalibrationRecord(BaseModel):
    """Tracks a prediction vs actual outcome for calibration."""
    id: Optional[str] = None
    market_id: str
    market_question: str = ""
    strategy: StrategyName = StrategyName.AI_PROBABILITY
    predicted_probability: float
    market_price_at_prediction: float
    actual_outcome: Optional[bool] = None  # None = unresolved
    predicted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: Optional[datetime] = None

    @property
    def is_resolved(self) -> bool:
        return self.actual_outcome is not None

    @property
    def brier_score(self) -> Optional[float]:
        """Brier score for this single prediction. Lower = better."""
        if self.actual_outcome is None:
            return None
        outcome = 1.0 if self.actual_outcome else 0.0
        return (self.predicted_probability - outcome) ** 2


# ──────────────────────────────────────────────
# Forecast Models (for AI probability engine)
# ──────────────────────────────────────────────

class ForecastResult(BaseModel):
    """Output from Claude's probability assessment."""
    probability: float
    confidence_low: float = 0.0
    confidence_high: float = 1.0
    key_factors_for: list[str] = Field(default_factory=list)
    key_factors_against: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    reasoning: str = ""
    model_used: str = ""
    tokens_used: int = 0
    latency_ms: int = 0
    raw_response: str = ""
    parse_failed: bool = False

    @field_validator("probability")
    @classmethod
    def probability_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"probability must be in [0.0, 1.0], got {v}")
        return v

    @field_validator("confidence_low")
    @classmethod
    def ci_low_range(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @field_validator("confidence_high")
    @classmethod
    def ci_high_valid(cls, v: float, info) -> float:
        import logging
        v = max(0.0, min(1.0, v))
        # Re-clamp ci_low defensively in case field validation order varies
        ci_low = max(0.0, min(1.0, info.data.get("confidence_low", 0.0)))
        if v < ci_low:
            # Auto-correct inverted CI bounds rather than rejecting
            # (Claude occasionally returns them swapped)
            logging.getLogger(__name__).warning(
                f"Inverted CI bounds: low={ci_low}, high={v} — swapping"
            )
            v = ci_low
        return v


class EnsembleForecast(BaseModel):
    """Aggregated forecast from multiple models/approaches."""
    final_probability: float
    individual_forecasts: list[ForecastResult] = Field(default_factory=list)
    market_price: float = 0.0
    edge: float = 0.0  # final_probability - market_price
    confidence: float = 0.5

    @field_validator("edge")
    @classmethod
    def edge_finite(cls, v: float) -> float:
        import math
        if not math.isfinite(v):
            raise ValueError(f"edge must be finite, got {v}")
        return v


# ──────────────────────────────────────────────
# Risk Models
# ──────────────────────────────────────────────

class RiskCheckResult(BaseModel):
    """Result from the risk engine's pre-trade checks."""
    passed: bool
    failed_checks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    approved_size: float = 0.0  # May be reduced from requested size


# ──────────────────────────────────────────────
# Whale Models (for Phase 6)
# ──────────────────────────────────────────────

class WhaleWallet(BaseModel):
    """A tracked whale wallet."""
    address: str
    alias: str = ""
    win_rate: float = 0.0
    total_pnl: float = 0.0
    total_trades: int = 0
    last_active: Optional[datetime] = None
    categories: list[str] = Field(default_factory=list)
    trusted: bool = True


class WhaleSignal(BaseModel):
    """A consensus signal from whale tracking."""
    market_id: str
    direction: Direction
    whale_count: int  # How many whales agree
    basket_size: int  # Total basket size
    consensus_pct: float  # whale_count / basket_size
    avg_entry_price: float = 0.0
    earliest_entry: Optional[datetime] = None
    wallets: list[str] = Field(default_factory=list)
