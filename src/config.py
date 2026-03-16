"""Configuration loader — reads settings.yaml and .env into typed Pydantic models."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class PolymarketConfig(BaseModel):
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_host: str = "https://data-api.polymarket.com"
    chain_id: int = 137
    signature_type: int = 1


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


class ClaudeConfig(BaseModel):
    model_primary: str = "claude-sonnet-4-6"
    model_highstakes: str = "claude-opus-4-6"
    highstakes_threshold: float = 50.0
    max_tokens: int = 2000
    temperature: float = 0.3
    max_assessments_per_cycle: int = 10


class AlertsConfig(BaseModel):
    enabled: bool = True
    imessage_enabled: bool = False
    daily_report_time: str = "21:00"


class DatabaseConfig(BaseModel):
    path: str = "data/markets.db"
    wal_mode: bool = True


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/logs/polyedge.log"


class Settings(BaseModel):
    """Root settings model — all configuration flows through here."""

    polymarket: PolymarketConfig = Field(default_factory=PolymarketConfig)
    scanning: ScanningConfig = Field(default_factory=ScanningConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # Secrets loaded from environment
    private_key: Optional[str] = None
    funder_address: Optional[str] = None
    anthropic_api_key: Optional[str] = None
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
    settings.private_key = os.environ.get("PRIVATE_KEY")
    settings.funder_address = os.environ.get("FUNDER_ADDRESS")
    settings.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    settings.live_enabled = os.environ.get("POLYEDGE_LIVE_ENABLED", "false").lower() == "true"

    return settings
