"""
断连重连守卫：指数退避 + 最大重试次数
"""
import asyncio
import structlog
import math

logger = structlog.get_logger(__name__)


class _Attempt:
    def __init__(self, n: int, base: float) -> None:
        self._delay = min(base * (2 ** n), 60.0)

    async def sleep(self) -> None:
        logger.info("Reconnecting", delay=self._delay)
        await asyncio.sleep(self._delay)


class ReconnectGuard:
    def __init__(self, max_retries: int = 20, base_delay: float = 1.0):
        self._max = max_retries
        self._base = base_delay
        self._n = 0

    def reset(self) -> None:
        self._n = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._n >= self._max:
            raise StopAsyncIteration
        attempt = _Attempt(self._n, self._base)
        self._n += 1
        return attempt
