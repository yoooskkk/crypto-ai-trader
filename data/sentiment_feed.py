"""
模块名称: sentiment_feed.py
所属层级: 数据采集层 (Data)
输入来源: Alternative.me Fear & Greed API / 公开情绪指数
输出去向: Redis Hash（sentiment:{metric}:{ts}），不走 Stream
关键依赖: aiohttp, structlog, redis

功能说明:
    获取市场情绪指标：Fear & Greed 指数。
    情绪数据是可选增强，不影响核心交易信号。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ─── 配置常量 ────────────────────────────────────────

FEAR_GREED_URL = "https://api.alternative.me/fng/"
SENTIMENT_REDIS_PREFIX = "sentiment:"
CACHE_TTL = 3600  # 1 小时


# ─── 数据结构 ──────────────────────────────────────────


@dataclass
class SentimentReading:
    """单一情绪读数"""
    metric: str          # 指标名: "fear_greed"
    value: int           # 数值 0~100
    classification: str  # 分类: "Extreme Fear" / "Fear" / "Neutral" / "Greed" / "Extreme Greed"
    timestamp: int       # Unix 时间戳（秒）
    source: str          # 数据来源

    @property
    def redis_key(self) -> str:
        return f"{SENTIMENT_REDIS_PREFIX}{self.metric}:{self.timestamp}"

    def to_redis_hash(self) -> dict[str, str]:
        return {
            "metric": self.metric,
            "value": str(self.value),
            "classification": self.classification,
            "ts": str(self.timestamp),
            "source": self.source,
        }


# ─── 情绪数据获取器 ──────────────────────────────────


class SentimentFeed:
    """
    市场情绪数据获取器。

    默认从 alternative.me Fear & Greed Index API 获取数据。
    结果写入 Redis Hash，key 格式: sentiment:{metric}:{timestamp}

    用法:
        feed = SentimentFeed()
        readings = await feed.fetch_fear_greed()
        for r in readings:
            print(f"{r.classification}: {r.value}")
    """

    def __init__(
        self,
        fng_url: str = FEAR_GREED_URL,
        timeout: int = 10,
        redis_client=None,
    ):
        self._fng_url = fng_url
        self._timeout = timeout
        self._redis = redis_client
        self._session = None
        self._own_redis = False

    async def _get_session(self):
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                headers={"Accept": "application/json"},
            )
        return self._session

    async def _get_redis(self):
        if self._redis is None:
            import os
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', 6379)}"
            )
            self._own_redis = True
        return self._redis

    async def close(self) -> None:
        """释放资源"""
        if self._session and not self._session.closed:
            await self._session.close()
        if self._own_redis and self._redis:
            await self._redis.aclose()

    @staticmethod
    def _classify_fg(value: int) -> str:
        """将 F&G 数值转换为分类标签"""
        if value <= 25:
            return "Extreme Fear"
        elif value <= 45:
            return "Fear"
        elif value <= 55:
            return "Neutral"
        elif value <= 75:
            return "Greed"
        else:
            return "Extreme Greed"

    async def fetch_fear_greed(
        self,
        limit: int = 7,
        store_redis: bool = False,
    ) -> list[SentimentReading]:
        """
        获取 Fear & Greed 指数。

        GET https://api.alternative.me/fng/?limit=N

        参数:
            limit: 返回天数（含今天），最大 30
            store_redis: 是否存储到 Redis

        返回:
            SentimentReading 列表，按时间降序。失败返回空列表
        """
        session = await self._get_session()
        params: dict[str, Any] = {"limit": min(limit, 30), "format": "json"}

        try:
            async with session.get(self._fng_url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("F&G API 请求失败", status=resp.status)
                    return []

                data = await resp.json()
                raw_list = data.get("data", [])

                if not raw_list:
                    logger.warning("F&G 返回空数据")
                    return []

                readings = []
                for item in raw_list[:limit]:
                    try:
                        value = int(item.get("value", 50))
                        ts = int(item.get("timestamp", 0))
                        classification = item.get("value_classification", self._classify_fg(value))

                        readings.append(SentimentReading(
                            metric="fear_greed",
                            value=value,
                            classification=classification,
                            timestamp=ts,
                            source="alternative.me",
                        ))
                    except (TypeError, ValueError) as e:
                        logger.warning("解析 F&G 条目失败", error=str(e))
                        continue

                if store_redis and readings:
                    await self._store_readings(readings)

                logger.info("获取 F&G 成功", count=len(readings))
                return readings

        except Exception as e:
            logger.error("抓取 F&G 异常", error=str(e))
            return []

    async def get_latest_fear_greed(
        self,
        store_redis: bool = False,
    ) -> SentimentReading | None:
        """
        获取最新 Fear & Greed 指数。

        参数:
            store_redis: 是否存储到 Redis

        返回:
            最新的 SentimentReading，失败返回 None
        """
        readings = await self.fetch_fear_greed(limit=1, store_redis=store_redis)
        return readings[0] if readings else None

    async def _store_readings(self, readings: list[SentimentReading]) -> None:
        """批量存储情绪数据到 Redis"""
        try:
            redis = await self._get_redis()
            async with redis.pipeline(transaction=False) as pipe:
                for reading in readings:
                    pipe.hset(reading.redis_key, mapping=reading.to_redis_hash())
                    pipe.expire(reading.redis_key, CACHE_TTL)
                await pipe.execute()
            logger.debug("情绪数据已存储到 Redis", count=len(readings))
        except Exception as e:
            logger.warning("存储情绪数据到 Redis 失败", error=str(e))
