"""
Binance WebSocket 客户端
- 订阅 K线/深度/归集成交
- 自动心跳 + 断连重连（委托 reconnect_guard）
- 数据写入 Redis Stream
"""
import asyncio
import json
import logging
from typing import Callable

import websockets

from messaging.producer import StreamProducer
from data.reconnect_guard import ReconnectGuard

logger = logging.getLogger(__name__)


class BinanceWSClient:
    BASE_URL = "wss://stream.binance.com:9443/ws"

    def __init__(self, symbols: list[str], interval: str = "1m"):
        self.symbols = symbols
        self.interval = interval
        self.producer = StreamProducer()
        self._guard = ReconnectGuard(max_retries=20, base_delay=1.0)

    async def run(self) -> None:
        streams = "/".join(
            f"{s.lower()}@kline_{self.interval}" for s in self.symbols
        )
        url = f"{self.BASE_URL}/{streams}"
        async for attempt in self._guard:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("WS connected: %s streams", len(self.symbols))
                    self._guard.reset()
                    async for raw in ws:
                        msg = json.loads(raw)
                        await self.producer.publish("raw_kline", msg)
            except Exception as exc:
                logger.warning("WS error: %s", exc)
                await attempt.sleep()
