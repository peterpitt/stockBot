"""
tests/test_strategy.py
───────────────────────
台股當沖系統 — 策略與時序單元測試
"""
from __future__ import annotations

import sys
from datetime import datetime, time
from pathlib import Path

import pytest
from zoneinfo import ZoneInfo

# ── 確保專案根目錄在 sys.path ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import get_settings
from src.utils.time_utils import TW_TZ, MARKET_OPEN, MARKET_CLOSE
from trader import is_limit_up


def test_settings_load() -> None:
    """測試系統設定載入與預設值"""
    settings = get_settings()
    assert settings.trading_env in ["simulation", "production"]
    assert settings.max_position_per_stock > 0
    assert settings.stop_loss_pct == 0.02
    assert settings.db_type in ["sqlite", "supabase"]
    assert settings.shioaji_ca_path.name == "Sinopac.pfx"


def test_is_limit_up() -> None:
    """測試漲停判定邏輯是否正確"""
    # 昨收 100 元，今日價格 110 元 → 剛好達漲停
    assert is_limit_up(110.0, 100.0) is True
    # 昨收 100 元，今日價格 109.9 元 → 剛好達漲停 (在 0.1% 容錯範圍內)
    assert is_limit_up(109.91, 100.0) is True
    # 昨收 100 元，今日價格 105 元 → 未達漲停
    assert is_limit_up(105.0, 100.0) is False
    # 參考價為 0 或負數 → 不判定漲停
    assert is_limit_up(100.0, 0.0) is False
    assert is_limit_up(100.0, -10.0) is False


def test_taiwan_timezone() -> None:
    """測試時區是否設定為 Asia/Taipei"""
    assert TW_TZ == ZoneInfo("Asia/Taipei")
    # 驗證時區偏移量為 UTC+8 (28800 秒)
    dt = datetime(2026, 5, 23, tzinfo=TW_TZ)
    assert dt.utcoffset().total_seconds() == 28800


def test_market_hours() -> None:
    """測試台股交易時間的定義是否正確"""
    assert MARKET_OPEN == time(9, 0)
    assert MARKET_CLOSE == time(13, 30)


def test_position_pnl_calculation() -> None:
    """測試 Position 的已實現損益計算（含手續費與稅）"""
    from src.strategy.position_manager import Position
    # 做空部位：空在 45.07，補在 45.27，張數 2 張
    pos = Position(
        position_id=1,
        stock_id="6547",
        stock_name="中菲行",
        strategy="short",
        entry_price=45.07,
        quantity=2,
        stop_loss=45.97,
    )
    pnl = pos.calc_pnl(45.27)
    # 預期損益：-929元 (精確計算約為 -929.09)
    assert round(pnl) == -929
