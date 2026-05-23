"""
src/scanner/tpex_scraper.py
────────────────────────────
非同步爬取上櫃（TPEx）投信每日買賣超數據。

TPEx 反爬機制：需先 GET 首頁取得 session cookie，再請求資料頁。

資料來源：
  https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php

輸出 DataFrame columns:
    stock_id, stock_name, trust_buy, trust_sell, trust_net, date
"""
from __future__ import annotations

import asyncio
import io
from datetime import date, timedelta
from typing import Optional

import aiohttp
import pandas as pd

from src.utils.logger import get_logger
from src.utils.retry import retry_async

logger = get_logger(__name__)

TPEX_HOME_URL = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge.php?l=zh-tw"
TPEX_DATA_URL = (
    "https://www.tpex.org.tw/web/stock/3insti/daily_trade/"
    "3itrade_hedge_result.php"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


class TPExScraper:
    """
    上櫃投信買賣超爬蟲。

    Usage:
        async with TPExScraper() as scraper:
            df = await scraper.fetch_daily(date(2024, 5, 20))
            history = await scraper.fetch_history(days=5)
    """

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._warmed_up = False

    async def __aenter__(self) -> "TPExScraper":
        self._session = aiohttp.ClientSession(
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            cookie_jar=aiohttp.CookieJar(),
        )
        return self

    async def __aexit__(self, *args) -> None:
        if self._session:
            await self._session.close()

    async def _warm_up_session(self) -> None:
        """先 GET 首頁，讓 TPEx 設置 session cookie，避免 403。"""
        if self._warmed_up:
            return
        try:
            assert self._session is not None
            async with self._session.get(
                TPEX_HOME_URL,
                headers={**HEADERS, "Referer": "https://www.tpex.org.tw/"},
            ) as resp:
                await resp.read()  # 消費 body，確保 cookie 被設置
                logger.info("tpex_session_warmed", status=resp.status)
            self._warmed_up = True
            await asyncio.sleep(0.5)  # 模擬真實瀏覽器行為
        except Exception as e:
            logger.warning("tpex_warmup_failed", error=str(e))

    async def _fetch_raw_csv(self, target_date: date) -> str:
        """回傳 TPEx 投信買賣超 CSV 原始文字。"""
        await self._warm_up_session()

        # TPEx 使用民國年格式：115/05/22
        roc_date = f"{target_date.year - 1911}/{target_date.month:02d}/{target_date.day:02d}"
        params = {"l": "zh-tw", "se": "EW", "t": "D", "o": "csv", "d": roc_date}

        async def _do_fetch() -> str:
            assert self._session is not None
            async with self._session.get(
                TPEX_DATA_URL,
                params=params,
                headers={
                    **HEADERS,
                    "Referer": TPEX_HOME_URL,
                    "X-Requested-With": "XMLHttpRequest",
                },
            ) as resp:
                if resp.status == 403:
                    # 403 通常是 session 過期，重置後重試
                    self._warmed_up = False
                    raise ConnectionError(f"TPEx 403 Forbidden，將重試（日期：{roc_date}）")
                resp.raise_for_status()
                raw = await resp.read()
                return raw.decode("big5", errors="replace")

        return await retry_async(_do_fetch, max_attempts=3)

    @staticmethod
    def _parse_csv(raw: str, target_date: date) -> pd.DataFrame:
        """解析 TPEx 原始 CSV → 標準化 DataFrame。"""
        lines = raw.splitlines()
        header_idx = next(
            (i for i, line in enumerate(lines) if "代號" in line or "代碼" in line),
            None,
        )
        if header_idx is None:
            logger.warning("tpex_parse_failed", date=str(target_date), reason="header not found")
            return pd.DataFrame()

        csv_body = "\n".join(lines[header_idx:])
        try:
            df = pd.read_csv(io.StringIO(csv_body), dtype=str, thousands=",")
        except Exception as e:
            logger.error("tpex_csv_parse_error", date=str(target_date), error=str(e))
            return pd.DataFrame()

        col_map: dict[str, str] = {}
        for col in df.columns:
            c = str(col).strip()
            if "代號" in c or "代碼" in c:         col_map[col] = "stock_id"
            elif "名稱" in c:                        col_map[col] = "stock_name"
            elif "投信" in c and "買進" in c:        col_map[col] = "trust_buy"
            elif "投信" in c and "賣出" in c:        col_map[col] = "trust_sell"
            elif "投信" in c and ("買賣超" in c or "淨買" in c): col_map[col] = "trust_net"

        df = df.rename(columns=col_map)

        required = {"stock_id", "stock_name", "trust_buy", "trust_sell", "trust_net"}
        if missing := required - set(df.columns):
            logger.warning("tpex_missing_columns", date=str(target_date), missing=list(missing))
            return pd.DataFrame()

        for num_col in ["trust_buy", "trust_sell", "trust_net"]:
            df[num_col] = (
                df[num_col].astype(str).str.replace(",", "", regex=False)
                .str.strip().replace({"--": "0", "": "0"})
                .astype(float).fillna(0).astype(int)
            )

        df["stock_id"] = df["stock_id"].astype(str).str.strip()
        df["stock_name"] = df["stock_name"].astype(str).str.strip()
        df["date"] = target_date
        df = df[df["stock_id"].str.match(r"^\d{4,6}[A-Z]?$", na=False)]
        df = df.reset_index(drop=True)

        logger.info(
            "tpex_fetched",
            date=str(target_date),
            rows=len(df),
            net_buy=(df["trust_net"] > 0).sum(),
            net_sell=(df["trust_net"] < 0).sum(),
        )
        return df

    async def fetch_daily(self, target_date: Optional[date] = None) -> pd.DataFrame:
        """爬取單一交易日的投信買賣超資料。"""
        if target_date is None:
            target_date = date.today() - timedelta(days=1)

        logger.info("tpex_fetching", date=str(target_date))
        raw = await self._fetch_raw_csv(target_date)
        return self._parse_csv(raw, target_date)

    async def fetch_history(self, days: int = 5) -> pd.DataFrame:
        """
        並發爬取過去 N 個交易日。
        注意：TPEx 有頻率限制，加入短暫延遲避免 ban。
        """
        today = date.today()
        fetch_dates: list[date] = []
        d = today - timedelta(days=1)
        while len(fetch_dates) < days:
            if d.weekday() < 5:
                fetch_dates.append(d)
            d -= timedelta(days=1)

        logger.info("tpex_history_start", dates=[str(d) for d in fetch_dates])

        # 串行執行（避免頻率限制），每次間隔 0.3s
        dfs: list[pd.DataFrame] = []
        for fetch_date in fetch_dates:
            try:
                df = await self.fetch_daily(fetch_date)
                if not df.empty:
                    dfs.append(df)
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error("tpex_history_error", date=str(fetch_date), error=str(e))

        if not dfs:
            return pd.DataFrame()

        combined = pd.concat(dfs, ignore_index=True)
        return combined.sort_values(["stock_id", "date"])
