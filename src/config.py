"""Configuration loader — reads settings.yaml and .env into typed Pydantic models."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator


class KalshiConfig(BaseModel):
    host: str = "https://api.elections.kalshi.com/trade-api/v2"
    demo_host: str = "https://demo-api.kalshi.co/trade-api/v2"
    use_demo: bool = True

    @property
    def active_host(self) -> str:
        return self.demo_host if self.use_demo else self.host


class PolymarketConfig(BaseModel):
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_host: str = "https://data-api.polymarket.com"
    chain_id: int = 137
    signature_type: int = 1  # 0=EOA, 1=proxy/email wallet
    enabled: bool = False


class ScanningConfig(BaseModel):
    interval_seconds: int = 300
    min_volume_24h: float = 10000
    min_liquidity: float = 5000
    max_markets: int = 200
    target_categories: list[str] = Field(default_factory=lambda: ["Politics", "Geopolitics", "Fed", "AI", "Tech"])
    exclude_categories: list[str] = Field(default_factory=lambda: ["Crypto Prices", "Sports"])


class TradingConfig(BaseModel):
    mode: str = "paper"
    bankroll: float = 500.0
    max_position_pct: float = 0.05
    max_total_exposure_pct: float = 0.40
    max_correlated_exposure_pct: float = 0.20
    min_edge_ai: float = 0.05
    min_edge_arb: float = 0.02
    min_edge_obvious_no: float = 0.01
    min_edge_news: float = 0.03  # Lower threshold: news edges are fast/temporary
    kelly_fraction: float = 0.5
    prefer_maker: bool = True
    daily_loss_limit_pct: float = 0.10
    max_drawdown_pct: float = 0.20  # H-3: Halt if equity drops >20% from peak
    max_obvious_no_pct: float = 0.10
    obvious_no_probability_multiplier: float = 0.3  # Conservative P(YES) scaling for obvious-NO markets.
    # The multiplier scales the market's YES price when estimating true P(YES):
    #   P(NO) = 1 - yes_price * multiplier
    # At 0.3: YES=$0.03 → P(NO)=0.991. Accounts for illiquidity inflating YES prices.
    # Calibrate against historical obvious-NO resolutions. Range: 0.1 (very conservative) to 0.5.
    max_trades_per_cycle: int = 5  # Max trades per scan cycle to prevent overtrading
    max_concurrent_positions: int = 6  # H-2: Hard cap on simultaneous open positions
    allow_position_additions: bool = True  # If False, block all trades on markets where a position already exists
    min_confidence: float = 0.55  # Minimum signal confidence required to trade

    @field_validator("mode")
    @classmethod
    def mode_valid(cls, v: str) -> str:
        if v not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got '{v}'")
        return v

    @field_validator("min_edge_ai", "min_edge_arb", "min_edge_obvious_no", "min_edge_news")
    @classmethod
    def min_edge_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"min_edge must be >= 0, got {v}")
        return v

    @field_validator("obvious_no_probability_multiplier")
    @classmethod
    def obvious_no_multiplier_valid(cls, v: float) -> float:
        if v <= 0 or v > 1:
            raise ValueError(
                f"obvious_no_probability_multiplier must be in (0, 1], got {v}. "
                f"Typical range is 0.1 (very conservative) to 0.5."
            )
        return v

    @field_validator("bankroll")
    @classmethod
    def bankroll_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("bankroll must be > 0")
        return v

    @field_validator("kelly_fraction")
    @classmethod
    def kelly_fraction_valid(cls, v: float) -> float:
        if v <= 0 or v > 1:
            raise ValueError("kelly_fraction must be in (0, 1]")
        return v

    @field_validator(
        "max_position_pct", "max_total_exposure_pct",
        "max_correlated_exposure_pct", "daily_loss_limit_pct",
        "max_obvious_no_pct",
    )
    @classmethod
    def pct_valid(cls, v: float) -> float:
        if v <= 0 or v > 1:
            raise ValueError("percentage fields must be in (0, 1]")
        return v


class ClaudeConfig(BaseModel):
    model_primary: str = "claude-sonnet-4-6"
    model_highstakes: str = "claude-opus-4-6"
    highstakes_threshold: float = 50.0
    max_tokens: int = 2000
    temperature: float = 0.3
    max_assessments_per_cycle: int = 10
    ensemble_weight: float = 0.85
    category_temperatures: dict[str, float] = Field(default_factory=lambda: {
        "Politics": 0.25,
        "Fed/Macro": 0.20,
        "Geopolitics": 0.30,
        "Tech/AI": 0.30,
        "Culture": 0.40,
    })
    cross_check_enabled: bool = True
    cross_check_top_n: int = 3
    cross_check_temp_low: float = 0.2
    cross_check_temp_high: float = 0.5
    cross_check_disagreement_threshold: float = 0.22
    decomposition_enabled: bool = True  # Enable multi-step decomposition for compound questions
    max_divergence_from_market: float = 0.40  # Reject if |claude - market| > this
    api_timeout_seconds: int = 60  # Hard timeout on Claude API calls
    daily_token_budget: int = 500_000  # Soft daily token budget warning threshold


class NewsConfig(BaseModel):
    rss_feeds: list[str] = Field(default_factory=lambda: [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.reuters.com/reuters/businessNews",
    ])
    poll_interval_seconds: int = 120
    min_relevance: float = 0.3
    max_article_age_minutes: int = 30
    serper_url: str = "https://google.serper.dev/search"
    staleness_thresholds: dict[str, int] = Field(default_factory=lambda: {
        "Fed/Macro": 5,
        "Tech/AI": 10,
        "Geopolitics": 7,
        "Politics": 14,
        "Culture": 30,
    })


class WhaleConfig(BaseModel):
    basket_path: str = "config/whale_basket.yaml"
    consensus_threshold: float = 0.8
    poll_interval_seconds: int = 600


class ExecutionConfig(BaseModel):
    stale_order_age_seconds: int = 1800  # Cancel orders resting > 30 min
    order_poll_timeout_seconds: int = 10  # Timeout for each order status check
    order_poll_delay_seconds: float = 2.0  # Delay between order status polls
    max_poll_attempts: int = 5  # Max order status poll attempts
    cycle_timeout_seconds: int = 300  # Hard timeout per scan-trade cycle
    # Exit thresholds (previously hardcoded in position_manager.py)
    stop_loss_pct: float = 0.30          # Exit if unrealized loss > 30% of cost basis
    max_hold_days: int = 21              # Exit if held > 21 days
    edge_gone_threshold: float = 0.20    # Exit if remaining edge < 20% of original
    trailing_stop_activate: float = 0.12 # Activate trailing stop after 12% gain
    trailing_stop_distance: float = 0.50 # Trail 50% of peak gain
    take_profit_pct: float = 0.80        # Take profit at 80% of max theoretical gain
    capital_rotation_edge: float = 0.40  # When exposure >35%, exit positions with <40% remaining edge


class AlertsConfig(BaseModel):
    enabled: bool = True
    imessage_enabled: bool = False
    imessage_endpoint: Optional[str] = None
    alert_on_trade: bool = True
    alert_on_circuit_breaker: bool = True
    daily_report_time: str = "21:00"
    dashboard_port: int = 8080


class DatabaseConfig(BaseModel):
    path: str = "data/markets.db"
    wal_mode: bool = True
    snapshot_retention_days: int = 30  # Cleanup snapshots older than this

    @field_validator("path")
    @classmethod
    def path_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Database path cannot be empty")
        return v


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/logs/polyedge.log"

    @field_validator("level")
    @classmethod
    def level_valid(cls, v: str) -> str:
        import logging
        if v.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"Invalid logging level: {v}")
        return v.upper()


class Settings(BaseModel):
    """Root settings model — all configuration flows through here."""

    kalshi: KalshiConfig = Field(default_factory=KalshiConfig)
    polymarket: PolymarketConfig = Field(default_factory=PolymarketConfig)
    scanning: ScanningConfig = Field(default_factory=ScanningConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    whales: WhaleConfig = Field(default_factory=WhaleConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # Secrets loaded from environment
    kalshi_api_key_id: Optional[str] = None
    kalshi_private_key_path: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    serper_api_key: Optional[str] = None
    searxng_url: Optional[str] = None
    fred_api_key: Optional[str] = None
    metaculus_api_token: Optional[str] = None
    polymarket_private_key: Optional[str] = None
    live_enabled: bool = False

    def validate_required_keys(self) -> list[str]:
        """Check for missing required API keys at startup. Returns list of warnings."""
        import logging
        _logger = logging.getLogger(__name__)
        warnings = []
        if not self.anthropic_api_key:
            warnings.append("ANTHROPIC_API_KEY not set — Claude forecasting will fail")
        if not self.kalshi_api_key_id or not self.kalshi_private_key_path:
            warnings.append("Kalshi API credentials not set — Kalshi trading disabled")
        if self.polymarket.enabled and not self.polymarket_private_key:
            warnings.append("Polymarket enabled but POLYMARKET_PRIVATE_KEY not set — PM trading will fail")
        for w in warnings:
            _logger.warning(w)
        return warnings


def load_settings(config_path: str | Path = "config/settings.yaml") -> Settings:
    """Load settings from YAML file and overlay environment variables."""
    # Load .env file (relative to config_path's parent directory)
    config_path = Path(config_path)
    env_path = config_path.parent / ".env"
    load_dotenv(env_path)

    # Load YAML if it exists
    yaml_data = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                yaml_data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise RuntimeError(f"Failed to parse config file {config_path}: {e}") from e

    # Build settings from YAML
    settings = Settings(**yaml_data)

    # Overlay secrets from environment
    settings.kalshi_api_key_id = os.environ.get("KALSHI_API_KEY_ID")
    settings.kalshi_private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    settings.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    settings.serper_api_key = os.environ.get("SERPER_API_KEY") or None
    settings.searxng_url = os.environ.get("SEARXNG_URL") or None
    settings.fred_api_key = os.environ.get("FRED_API_KEY") or None
    settings.metaculus_api_token = os.environ.get("METACULUS_API_TOKEN") or None
    settings.polymarket_private_key = os.environ.get("POLYMARKET_PRIVATE_KEY") or None
    settings.live_enabled = os.environ.get("POLYEDGE_LIVE_ENABLED", "false").lower() == "true"

    return settings
