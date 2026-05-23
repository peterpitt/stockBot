"""
src/utils/time_utils.py
───────────────────────
台股交易時段判斷工具。台股：09:00-13:30，時區 Asia/Taipei (UTC+8)
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

TW_TZ = ZoneInfo("Asia/Taipei")

# ── 時段定義 ──────────────────────────────────────────────────────────────────
MARKET_OPEN     = time(9, 0)
MARKET_CLOSE    = time(13, 30)
PRE_OPEN        = time(8, 30)    # 開盤前準備
FORCE_CLOSE_AT  = time(13, 25)  # 強制平倉時間
SCANNER_RUN_AT  = time(20, 0)   # 盤後選股排程時間


def now_tw() -> datetime:
    """取得當前台灣時間（帶時區）"""
    return datetime.now(tz=TW_TZ)


def is_trading_hours() -> bool:
    """是否在盤中時段"""
    t = now_tw().time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def is_pre_open() -> bool:
    """是否在開盤前準備時段"""
    t = now_tw().time()
    return PRE_OPEN <= t < MARKET_OPEN


def should_force_close() -> bool:
    """是否達到強制平倉時間"""
    t = now_tw().time()
    return t >= FORCE_CLOSE_AT


def minutes_to_open() -> float:
    """距離開盤幾分鐘（負值表示已開盤）"""
    now = now_tw()
    open_dt = now.replace(hour=9, minute=0, second=0, microsecond=0)
    return (open_dt - now).total_seconds() / 60


def is_weekday() -> bool:
    """是否為週一至週五（不含國定假日，需另行處理）"""
    return now_tw().weekday() < 5


def trading_date() -> str:
    """取得交易日日期字串 YYYY-MM-DD"""
    return now_tw().strftime("%Y-%m-%d")
