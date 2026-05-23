"""
config/settings.py
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",   # ← 忽略 .env 裡任何多餘的 key，不報錯
    )

    # ── Shioaji（全部 Optional，scanner 模式不需要）──────────────────────────
    shioaji_api_key: Optional[str] = None
    shioaji_secret_key: Optional[str] = None
    shioaji_account_id: Optional[str] = None
    shioaji_ca_path: Path = _PROJECT_ROOT / "certs" / "Sinopac.pfx"
    shioaji_ca_passwd: Optional[str] = None

    # ── 交易環境 ──────────────────────────────────────────────────────────────
    trading_env: Literal["simulation", "production"] = "simulation"
    max_position_per_stock: int = 100_000
    max_total_exposure: int = 500_000
    stop_loss_pct: float = 0.02
    force_close_time: str = "13:25"

    # ── 資料庫 ────────────────────────────────────────────────────────────────
    db_type: Literal["sqlite", "supabase"] = "sqlite"
    sqlite_path: Path = _PROJECT_ROOT / "data" / "processed" / "trades.db"
    supabase_url: str = ""
    supabase_key: str = ""

    # ── 選股參數 ──────────────────────────────────────────────────────────────
    trust_buy_consecutive_days: int = 3
    trust_buy_ratio_pct: float = 0.005
    branch_buy_threshold: int = 500

    daytrade_branches: list[str] = [
        "凱基台北", "凱基士林", "元富", "元富台中",
        "群益金鼎", "群益台中", "永豐金", "永豐金中壢",
        "富邦", "富邦中山", "國泰", "國泰台北",
        "兆豐", "統一", "台新",
    ]

    # ── 日誌 ──────────────────────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_dir: Path = _PROJECT_ROOT / "logs"

    @field_validator("log_dir", mode="before")
    @classmethod
    def ensure_log_dir(cls, v: str | Path) -> Path:
        p = Path(v)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def project_root(self) -> Path:
        return _PROJECT_ROOT

    @property
    def branch_csv_path(self) -> Path:
        return _PROJECT_ROOT / "data" / "raw" / "branch_data.csv"

    @property
    def watchlist_path(self) -> Path:
        return _PROJECT_ROOT / "data" / "processed" / "watchlist.json"

    @property
    def is_production(self) -> bool:
        return self.trading_env == "production"

    def require_shioaji(self) -> None:
        """trader.py 呼叫，確保交易憑證齊全才能下單。"""
        missing = [
            name for name, val in [
                ("SHIOAJI_API_KEY",    self.shioaji_api_key),
                ("SHIOAJI_SECRET_KEY", self.shioaji_secret_key),
                ("SHIOAJI_ACCOUNT_ID", self.shioaji_account_id),
                ("SHIOAJI_CA_PASSWD",  self.shioaji_ca_passwd),
            ] if not val
        ]
        if missing:
            raise EnvironmentError(
                f"交易模式缺少以下 .env 設定：\n  " + "\n  ".join(missing)
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
