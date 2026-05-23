"""
src/utils/logger.py
───────────────────
structlog 結構化日誌：JSON 格式輸出，支援 rotation。
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog

_initialized = False


def setup_logging(log_level: str = "INFO", log_dir: Path = Path("./logs")) -> None:
    global _initialized
    if _initialized:
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # ── stdlib handler: rotating file (JSON) ─────────────────────────────────
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_dir / "daytrade.log",
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("%(message)s"))

    # ── stdlib handler: console (human-readable) ──────────────────────────────
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    logging.basicConfig(
        level=numeric_level,
        handlers=[file_handler, console_handler],
    )

    # ── structlog processors ──────────────────────────────────────────────────
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _initialized = True


def get_logger(name: str) -> structlog.BoundLogger:
    """取得 structlog logger，自動帶入 module 名稱。"""
    return structlog.get_logger(name)
