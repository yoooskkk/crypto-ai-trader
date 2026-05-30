#!/usr/bin/env python3
"""临时脚本：重写 data/ws_client.py"""
content = '''"""
Binance WebSocket 客户端
- 订阅 K线/深度/归集成交
- 自动心跳 + 断连重连（委托 reconnect_guard）
- 数据写入 Redis Stream
- 统一使用 structlog 日志
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
import websockets

from data.reconnect_guard import ReconnectGuard
from messaging.producer import StreamProducer

logger = structlog.get_logger(__name__)


class BinanceWSClient:
    """
    Binance WebSocket 客户端。

    用法:
        client = BinanceWSClient(symbols=["BTCUSDT", "ETHUSDT"], interval="1m")
        await client.run()
    """

    BASE_URL = "wss://stream.binance.com:9443/ws"
    MAX_STREAMS_PER_CONNECTION = 200  # Binance 限制

    def __init__(
        self,
        symbols: list[str],
        interval: str = "1m",
        streams: list[str] | None = None,
    ):
        """
        参数:
            symbols: 交易对列表，如 ["BTCUSDT", "ETHUSDT"]
            interval: K 线周期，默认 "1m"
            streams: 自定义 stream 名列表（覆盖默认的 kline stream）
        """
        self.symbols = [s.upper() for s in symbols]
        self.interval = interval
        self._custom_streams = streams
        self.producer = StreamProducer()
        self._guard = ReconnectGuard(max_retries=20, base_delay=1.0)
        self._running = False

    # ─── 消息解析 ─────────────────────────────────

    @staticmethod
    def parse_kline_message(raw: dict) -> dict[str, Any] | None:
        """
        解析 Binance K 线 WebSocket 消息。

        Binance WS K 线格式:
        {
            "e": "kline",         // 事件类型
            "E": 123456789,       // 事件时间
            "s": "BTCUSDT",       // 交易对
            "k": {
                "t": 123400000,   // K 线开始时间
                "T": 123460000,   // K 线结束时间
                "s": "BTCUSDT",   // 交易对
                "i": "1m",        // 周期
                "f": 100,         // 第一笔成交 ID
                "L": 200,         // 最后一笔成交 ID
                "o": "42000.00",  // 开盘价
                "c": "42100.00",  // 收盘价
                "h": "42500.00",  // 最高价
                "l": "41800.00",  // 最低价
                "v": "1234.56",   // 成交量
                "n": 12345,       // 成交笔数
                "x": false,       // K 线是否已完结
                "q": "5000000",   // 成交额
                "V": "600.00",    // taker 买入成交量
                "Q": "2500000",   // taker 买入成交额
                "B": "0"          // 忽略
            }
        }
        """
        if not isinstance(raw, dict):
            logger.warning("WS 消息非 dict 格式", type=type(raw).__name__)
            return None

        event_type = raw.get("e", "")
        if event_type != "kline":
            return None

        k = raw.get("k")
        if not isinstance(k, dict):
            logger.warning("WS K 线消息缺少 k 字段")
            return None

        try:
            return {
                "symbol": k.get("s", ""),
                "timeframe": k.get("i", ""),
                "ts": int(k.get("t", 0)),
                "open": k.get("o", "0"),
                "high": k.get("h", "0"),
                "low": k.get("l", "0"),
                "close": k.get("c", "0"),
                "volume": k.get("v", "0"),
                "quote_volume": k.get("q", "0"),
                "trades": int(k.get("n", 0)),
                "is_closed": bool(k.get("x", False)),
                "taker_buy_volume": k.get("V", "0"),
                "taker_buy_quote": k.get("Q", "0"),
                "event_time": int(raw.get("E", 0)),
            }
        except (TypeError, ValueError) as e:
            logger.warning("WS K 线字段解析失败", error=str(e), kline=k)
            return None

    # ─── 主循环 ───────────────────────────────────

    async def run(self) -> None:
        """启动 WebSocket 连接主循环（自动重连）"""
        self._running = True
        streams = self._build_streams()
        url = f"{self.BASE_URL}/{streams}"

        async for attempt in self._guard:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info(
                        "WS 连接成功",
                        symbols_count=len(self.symbols),
                        interval=self.interval,
                    )
                    self._guard.reset()
                    async for raw in ws:
                        msg = json.loads(raw)
                        await self._handle_message(msg)
            except asyncio.CancelledError:
                logger.info("WS 客户端收到取消信号")
                self._running = False
                break
            except Exception as exc:
                logger.warning("WS 连接异常，即将重连", error=str(exc))
                await attempt.sleep()

    async def stop(self) -> None:
        """停止 WebSocket 客户端"""
        self._running = False

    def _build_streams(self) -> str:
        """构建 Binance 组合 stream URL 路径"""
        if self._custom_streams:
            return "/".join(self._custom_streams)

        return "/".join(
            f"{s.lower()}@kline_{self.interval}" for s in self.symbols
        )

    async def _handle_message(self, msg: dict) -> None:
        """处理单条 WebSocket 消息"""
        event_type = msg.get("e", "")

        if event_type == "kline":
            parsed = self.parse_kline_message(msg)
            if parsed:
                await self.producer.publish("raw_kline", parsed)
        elif event_type == "depthUpdate":
            await self.producer.publish("raw_depth", msg)
        elif event_type == "aggTrade":
            await self.producer.publish("raw_trade", msg)
        else:
            logger.debug("WS 未识别事件类型", event_type=event_type)
'''

with open("data/ws_client.py", "w", encoding="utf-8") as f:
    f.write(content)
print("ws_client.py 写入完成")
