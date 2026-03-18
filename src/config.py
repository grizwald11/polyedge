"""Configuration loader — reads settings.yaml and .env into typed Pydantic models."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator


class KalshiConfig(BaseModel):
    host: str = "https://api.elections.kalshi.com/trade-api/v2"
    demo_host: str = "https://demo-api.kalshi.co/trade-api/v2"
    use_demo: bool = True

    @property
    def active_host(self) -> str:
        return self.demo_host if self.use_demo else self.host


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
    kelly_fraction: float = 0.5
    prefer_maker: bool = True
    daily_loss_limit_pct: float = 0.10
    max_obvious_no_pct: float = 0.10
    max_trades_per_cycle: int = 5  # Max trades per scan cycle to prevent overtrading

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
    category_temperatures: dict[str, float] = Field(default_factory=dict)
    cross_check_enabled: bool = False
    cross_check_top_n: int = 3
    cross_check_temp_low: float = 0.2
    cross_check_temp_high: float = 0.5
    cross_check_disagreement_threshold: float = 0.15


class NewsConfig(BaseModel):
    rss_feeds: list[str] = Field(default_factory=lambda: [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.reuters.com/reuters/businessNews",
    ])
    poll_interval_seconds: int = 120
    min_relevance: float = 0.3
    max_article_age_minutes: int = 30


class WhaleConfig(BaseModel):
    basket_path: str = "config/whale_basket.yaml"
    consensus_threshold: float = 0.8
    poll_interval_seconds: int = 600


class AlertsConfig(BaseModel):
    enabled: bool = True
    imessage_enabled: bool = False
    imessage_endpoint: Optional[str] = None
    alert_on_trade: bool = True
    alert_on_circuit_breaker: bool = True
    daily_report_time: str = "21:00"


class DatabaseConfig(BaseModel):
    path: str = "data/markets.db"
    wal_mode: bool = True


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/logs/polyedge.log"


class Settings(BaseModel):
    """Root settings model — all configuration flows through here."""

    kalshi: KalshiConfig = Field(default_factory=KalshiConfig)
    scanning: ScanningConfig = Field(default_factory=ScanningConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    whales: WhaleConfig = Field(default_factory=WhaleConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # Secrets loaded from environment
    kalshi_api_key_id: Optional[str] = None
    kalshi_private_key_path: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    serper_api_key: Optional[str] = None
    fred_api_key: Optional[str] = None
    live_enabled: bool = False


def load_settings(config_path: str | Path = "config/settings.yaml") -> Settings:
    """Load settings from YAML file and overlay environment variables."""
    config_path = Path(config_path)

    # Load YAML if it exists
    yaml_data = {}
    if config_path.exists():
        with open(config_path) as f:
            yaml_data = yaml.safe_load(f) or {}

    # Build settings from YAML
    settings = Settings(**yaml_data)

    # Overlay secrets from environment
    settings.kalshi_api_key_id = os.environ.get("KALSHI_API_KEY_ID")
    settings.kalshi_private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    settings.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    settings.serper_api_key = os.environ.get("SERPER_API_KEY") or None
    settings.fred_api_key = os.environ.get("FRED_API_KEY") or None
    settings.live_enabled = os.environ.get("POLYEDGE_LIVE_ENABLED", "false").lower() == "true"

    return settings
