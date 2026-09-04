from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


class RiskConfig(BaseModel):
    max_position_pct: float = 0.12
    max_open_positions: int = 8
    max_daily_loss_pct: float = 0.04
    max_drawdown_pct: float = 0.12
    stop_loss_pct: float = 0.018
    take_profit_pct: float = 0.035
    trailing_stop_pct: float = 0.012
    min_confidence: float = 0.58
    max_spread_bps: float = 25
    cooldown_after_loss_sec: float = 90
    fee_bps: float = 10
    slippage_bps: float = 5


class ExchangeEndpoint(BaseModel):
    enabled: bool = True
    ws: str = ""
    rest: str = ""


class Settings(BaseModel):
    mode: str = "paper"
    starting_equity: float = 10_000
    quote_asset: str = "USDT"
    loop_interval_sec: float = 2.0
    candle_interval: str = "1m"
    max_watch_symbols: int = 40
    watchlist: list[str] = Field(default_factory=list)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    exchanges: dict[str, ExchangeEndpoint] = Field(default_factory=dict)
    strategies: dict[str, dict[str, Any]] = Field(default_factory=dict)
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def is_live(self) -> bool:
        return self.mode.lower() == "live"


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> Settings:
    cfg_path = ROOT / "config.yaml"
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    env_overlay: dict[str, Any] = {}
    if os.getenv("BOT_MODE"):
        env_overlay["mode"] = os.getenv("BOT_MODE")
    if os.getenv("STARTING_EQUITY"):
        env_overlay["starting_equity"] = float(os.getenv("STARTING_EQUITY"))
    if os.getenv("BOT_HOST"):
        env_overlay["host"] = os.getenv("BOT_HOST")
    if os.getenv("BOT_PORT"):
        env_overlay["port"] = int(os.getenv("BOT_PORT"))

    merged = _deep_merge(raw, env_overlay)
    return Settings.model_validate(merged)


def api_keys() -> dict[str, dict[str, str]]:
    return {
        "binance": {
            "key": os.getenv("BINANCE_API_KEY", ""),
            "secret": os.getenv("BINANCE_API_SECRET", ""),
        },
        "bybit": {
            "key": os.getenv("BYBIT_API_KEY", ""),
            "secret": os.getenv("BYBIT_API_SECRET", ""),
        },
        "okx": {
            "key": os.getenv("OKX_API_KEY", ""),
            "secret": os.getenv("OKX_API_SECRET", ""),
            "passphrase": os.getenv("OKX_PASSPHRASE", ""),
        },
        "coinbase": {
            "key": os.getenv("COINBASE_API_KEY", ""),
            "secret": os.getenv("COINBASE_API_SECRET", ""),
        },
        "kraken": {
            "key": os.getenv("KRAKEN_API_KEY", ""),
            "secret": os.getenv("KRAKEN_API_SECRET", ""),
        },
    }


def has_live_keys() -> bool:
    keys = api_keys()
    return any(v.get("key") and v.get("secret") for v in keys.values())
