"""
src/utils/retry.py
──────────────────
tenacity 重試裝飾器：針對網路與 API 斷線場景調校。
"""
from __future__ import annotations

import asyncio
from typing import Callable, Type

from tenacity import (
    AsyncRetrying,
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random,
    before_sleep_log,
    after_log,
)
import logging

logger = logging.getLogger(__name__)

# ── 針對 Shioaji API 的標準重試策略 ──────────────────────────────────────────
shioaji_retry = retry(
    retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    wait=wait_exponential(multiplier=1, min=2, max=30) + wait_random(0, 2),
    stop=stop_after_attempt(5),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    after=after_log(logger, logging.INFO),
    reraise=True,
)

# ── 針對 HTTP 爬蟲的重試策略 ─────────────────────────────────────────────────
http_retry = retry(
    retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    wait=wait_exponential(multiplier=2, min=3, max=60),
    stop=stop_after_attempt(3),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


async def retry_async(
    coro_fn: Callable,
    *args,
    max_attempts: int = 5,
    exc_types: tuple[Type[Exception], ...] = (ConnectionError, TimeoutError, OSError),
    **kwargs,
):
    """
    通用非同步重試執行器。
    
    Usage:
        result = await retry_async(my_async_func, arg1, kwarg=val)
    """
    async for attempt in AsyncRetrying(
        retry=retry_if_exception_type(exc_types),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(max_attempts),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    ):
        with attempt:
            return await coro_fn(*args, **kwargs)
