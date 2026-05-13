"""
数据缺口补全
WS 断连恢复后，用 REST API 补全缺失的 K 线
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class GapFiller:
    def __init__(self, rest_client):
        self.rest = rest_client
        self._last_ts: dict[str, int] = {}

    async def fill(self, symbol: str, interval: str, current_ts: int) -> list[dict]:
        last = self._last_ts.get(f"{symbol}_{interval}")
        if last is None:
            self._last_ts[f"{symbol}_{interval}"] = current_ts
            return []
        gap_ms = current_ts - last
        interval_ms = self._interval_to_ms(interval)
        if gap_ms <= interval_ms * 1.5:
            self._last_ts[f"{symbol}_{interval}"] = current_ts
            return []
        logger.warning("Gap detected %s %s: %dms", symbol, interval, gap_ms)
        klines = await self.rest.get_klines(symbol, interval, start=last, end=current_ts)
        self._last_ts[f"{symbol}_{interval}"] = current_ts
        return klines

    @staticmethod
    def _interval_to_ms(interval: str) -> int:
        units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
        return int(interval[:-1]) * units[interval[-1]]
