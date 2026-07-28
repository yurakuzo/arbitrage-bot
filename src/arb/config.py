"""Configuration loading.

Two sources, kept separate on purpose:
- Secrets & environment  -> `.env` / process env, loaded via Settings (pydantic).
- Non-secret tuning       -> `config/config.yaml`, loaded via AppConfig.

Nothing here reads network or writes files at import time.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config" / "config.example.yaml"


class Settings(BaseSettings):
    """Secrets & runtime environment (from `.env` / env vars, prefix ARB_)."""

    model_config = SettingsConfigDict(
        env_prefix="ARB_",
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "demo"  # demo | prod
    db_path: str = "data/arb.sqlite"

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: str | None = None

    polymarket_private_key: str | None = None

    # HARD master switch for real order placement. Must be explicitly true AND
    # combined with a non-paper execution mode AND prod environment before any
    # real order can be sent (see TradingGate). Default false = paper only.
    live_trading: bool = False

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


class ThresholdConfig(BaseModel):
    """Tuning knobs for detection & sizing."""

    min_edge: float = 0.01  # min fee-net edge (dollars per contract) to flag
    # Quotes older than this are stale -> anomaly. Must exceed the feed cadence
    # (Kalshi polls ~3s; WS re-emits ~2s), or healthy feeds false-trigger.
    max_quote_age_ms: int = 8000
    max_stake_usd: float = 100.0  # per-opportunity cap (paper)


class VenueDiscovery(BaseModel):
    """How to find candidate markets for a venue in analysis mode."""

    # Kalshi: series tickers to sweep (each expands to its recurring markets).
    series: list[str] = Field(default_factory=list)
    # Kalshi: whole categories to sweep (resolved to their series via /series).
    categories: list[str] = Field(default_factory=list)
    # Max markets listed per series.
    per_series_limit: int = 100


class DiscoveryConfig(BaseModel):
    """Per-venue discovery. Kalshi's listing is flooded by auto-generated sports
    markets, so it needs curated series; Polymarket's listing is volume-sorted
    and usable as-is."""

    # Default seeds demonstrate the recurring-weather use case out of the box.
    kalshi: VenueDiscovery = Field(
        default_factory=lambda: VenueDiscovery(series=["KXHIGHNY", "KXHIGHLAX", "KXHIGHCHI"])
    )
    polymarket: VenueDiscovery = Field(default_factory=VenueDiscovery)


class ExecutionConfig(BaseModel):
    """Live-execution behaviour (Phase 4). Defaults are the safest possible."""

    # paper: simulate only (no real orders — the v1 default and always safe).
    # semi_auto: propose each trade via Telegram and place only on confirmation.
    # auto: place immediately (highest risk; requires the master switch + prod).
    mode: str = "paper"
    confirm_timeout_s: int = 120  # semi_auto: how long to wait for a Telegram yes
    max_order_stake_usd: float = 50.0  # hard per-order cap on real capital


class AppConfig(BaseModel):
    """Non-secret configuration loaded from YAML."""

    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    # Enabled venues for this deployment.
    venues: list[str] = Field(default_factory=lambda: ["kalshi", "polymarket"])


def load_app_config(path: Path | None = None) -> AppConfig:
    """Load YAML config, falling back to the checked-in example, then defaults."""
    for candidate in (path, DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG_PATH):
        if candidate and candidate.exists():
            data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            return AppConfig.model_validate(data)
    return AppConfig()


@lru_cache
def get_settings() -> Settings:
    return Settings()
