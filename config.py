"""Configuration loader for the Kalshi Market Maker."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class ApiConfig:
    key_id: str = ""
    private_key_path: str = ""
    environment: str = "demo"

    @property
    def base_url(self) -> str:
        if self.environment == "prod":
            return "https://api.elections.kalshi.com/trade-api/v2"
        return "https://demo-api.kalshi.co/trade-api/v2"


@dataclass
class ScannerConfig:
    target_series: list[str] = field(default_factory=list)
    target_tickers: list[str] = field(default_factory=list)
    min_volume_24h: int = 100
    min_open_interest: int = 50
    max_markets: int = 5
    min_time_to_expiry_sec: int = 3600


@dataclass
class PricingConfig:
    default_spread_cents: int = 4
    min_spread_cents: int = 2
    max_spread_cents: int = 10
    min_price: int = 5
    max_price: int = 95


@dataclass
class QuotingConfig:
    order_size: int = 10
    max_orders_per_side: int = 1
    requote_threshold_cents: int = 1
    order_expiration_sec: int = 120


@dataclass
class RiskConfig:
    max_position_per_market: int = 100
    max_total_exposure_dollars: float = 500.0
    skew_cents_per_contract: float = 0.1


@dataclass
class LoopConfig:
    poll_interval_sec: float = 5.0


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_file: str = "logs/market_maker.log"


@dataclass
class Config:
    api: ApiConfig = field(default_factory=ApiConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    quoting: QuotingConfig = field(default_factory=QuotingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _build_dataclass(cls, data: dict):
    """Build a dataclass instance from a dict, ignoring unknown keys."""
    if data is None:
        return cls()
    known = {f.name for f in cls.__dataclass_fields__.values()}
    filtered = {k: v for k, v in data.items() if k in known}
    return cls(**filtered)


def load_config(path: Optional[str] = None) -> Config:
    """Load configuration from a YAML file with environment variable overrides."""
    if path is None:
        path = os.environ.get("MM_CONFIG_PATH", "config.yaml")

    config_path = Path(path)
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
    else:
        raw = {}

    cfg = Config(
        api=_build_dataclass(ApiConfig, raw.get("api")),
        scanner=_build_dataclass(ScannerConfig, raw.get("scanner")),
        pricing=_build_dataclass(PricingConfig, raw.get("pricing")),
        quoting=_build_dataclass(QuotingConfig, raw.get("quoting")),
        risk=_build_dataclass(RiskConfig, raw.get("risk")),
        loop=_build_dataclass(LoopConfig, raw.get("loop")),
        logging=_build_dataclass(LoggingConfig, raw.get("logging")),
    )

    # Environment variable overrides
    if os.environ.get("KALSHI_API_KEY_ID"):
        cfg.api.key_id = os.environ["KALSHI_API_KEY_ID"]
    if os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        cfg.api.private_key_path = os.environ["KALSHI_PRIVATE_KEY_PATH"]
    if os.environ.get("KALSHI_ENVIRONMENT"):
        cfg.api.environment = os.environ["KALSHI_ENVIRONMENT"]

    return cfg
