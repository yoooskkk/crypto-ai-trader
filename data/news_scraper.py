"""
模块名称: news_scraper.py
所属层级: 数据采集层 (Data)
输入来源: CryptoPanic / 公开新闻 API
输出去向: Redis Hash（news:{symbol}:{timestamp}），不走 Stream
关键依赖: aiohttp, structlog, redis

功能说明:
    抓取加密货币相关新闻，结构化存入 Redis。
    新闻是可选增强，不影响核心交易信号。
    抓取失败时记录警告跳过，不中断主流程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ─── 配置常量 ────────────────────────────────────────

DEFAULT_CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"
NEWS_REDIS_PREFIX = "news:"
CACHE_TTL = 3600  # 1 小时


# ─── 数据结构 ──────────────────────────────────────────


@dataclass
class NewsItem:
    """新闻条目"""
    title: str
    source: str            # 来源名称，如 "CryptoPanic", "Twitter"
    url: str
    published_at: int      # Unix 时间戳（秒）
    sentiment_score: float | None = None  # -1.0 ~ 1.0
    summary: str = ""
    symbols: list[str] = field(default_factory=list)  # 关联币种

    @property
    def redis_key(self) -> str:
        """生成 Redis key"""
        for sym in self.symbols or ["general"]:
            return f"{NEWS_REDIS_PREFIX}{sym}:{self.published_at}"
        return f"{NEWS_REDIS_PREFIX}general:{self.published_at}"

    def to_redis_hash(self) -> dict[str, str]:
        """转换为 Redis Hash 字段"""
        return {
            "title": self.title,
            "source": self.source,
            "url": self.url,
            "ts": str(self.published_at),
            "sentiment": str(self.sentiment_score) if self.sentiment_score is not None else "",
            "summary": self.summary,
            "symbols": ",".join(self.symbols),
        }


# ─── 新闻抓取器 ──────────────────────────────────────


class NewsScraper:
    """
    加密货币新闻抓取器。

    默认从 CryptoPanic 公开 API 获取新闻，支持扩展其他来源。
    抓取结果写入 Redis Hash，key 格式: news:{symbol}:{timestamp}

    用法:
        scraper = NewsScraper()
        news = await scraper.fetch_latest(api_key="...")
        for item in news:
            print(item.title, item.sentiment_score)
    """

    def __init__(
        self,
        api_url: str = DEFAULT_CRYPTOPANIC_URL,
        timeout: int = 10,
        redis_client=None,
    ):
        """
        参数:
            api_url: 新闻 API 地址
            timeout: HTTP 请求超时（秒）
            redis_client: Redis 客户端，为 None 时惰性创建
        """
        self._api_url = api_url
        self._timeout = timeout
        self._redis = redis_client
        self._session = None
        self._own_redis = False

    async def _get_session(self):
        """惰性创建 aiohttp session"""
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                headers={"Accept": "application/json"},
            )
        return self._session

    async def _get_redis(self):
        """惰性获取 Redis 客户端"""
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

    async def fetch_latest(
        self,
        api_key: str | None = None,
        currencies: str | None = None,
        limit: int = 25,
        store_redis: bool = False,
    ) -> list[NewsItem]:
        """
        获取最新新闻。

        参数:
            api_key: CryptoPanic API Key（公开 API 可选，但建议提供）
            currencies: 过滤币种（逗号分隔），如 "BTC,ETH"
            limit: 返回条数（最大 50）
            store_redis: 是否存储到 Redis

        返回:
            NewsItem 列表，按时间降序。失败返回空列表
        """
        if not api_key:
            logger.warning("未提供 API Key，使用无认证请求")

        session = await self._get_session()
        params: dict[str, Any] = {"limit": min(limit, 50)}
        if api_key:
            params["auth_token"] = api_key
        if currencies:
            params["currencies"] = currencies

        try:
            async with session.get(self._api_url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("新闻 API 请求失败", status=resp.status)
                    return []

                data = await resp.json()
                results = data.get("results", []) if isinstance(data, dict) else data

                if not results:
                    return []

                news_list = []
                for item in results[:limit]:
                    try:
                        news = self._parse_item(item)
                        if news:
                            news_list.append(news)
                    except Exception as e:
                        logger.warning("解析新闻条目失败", error=str(e))
                        continue

                if store_redis and news_list:
                    await self._store_news(news_list)

                logger.info("获取新闻成功", count=len(news_list))
                return news_list

        except Exception as e:
            logger.error("抓取新闻异常", error=str(e))
            return []

    @staticmethod
    def _parse_item(item: dict) -> NewsItem | None:
        """解析单条 CryptoPanic 新闻条目"""
        title = item.get("title", "")
        if not title:
            return None

        source = item.get("source", {})
        domain = source.get("domain", "unknown") if isinstance(source, dict) else str(source)

        url = item.get("url", "")
        published = item.get("published_at", "")

        import datetime
        if published:
            try:
                dt = datetime.datetime.fromisoformat(published.replace("Z", "+00:00"))
                published_ts = int(dt.timestamp())
            except (ValueError, TypeError):
                published_ts = int(datetime.datetime.now().timestamp())
        else:
            published_ts = int(datetime.datetime.now().timestamp())

        # CryptoPanic 的投票分数 (-1 ~ 1)
        votes = item.get("votes", {})
        if isinstance(votes, dict):
            positive = votes.get("positive", 0)
            negative = votes.get("negative", 0)
            total = positive + negative
            sentiment = (positive - negative) / total if total > 0 else None
        else:
            sentiment = None

        # 关联币种
        currencies_data = item.get("currencies", [])
        symbols = []
        if isinstance(currencies_data, list):
            for c in currencies_data:
                code = c.get("code", "") if isinstance(c, dict) else str(c)
                if code:
                    symbols.append(code.upper())

        return NewsItem(
            title=title,
            source=domain,
            url=url,
            published_at=published_ts,
            sentiment_score=sentiment,
            symbols=symbols,
        )

    async def _store_news(self, news_list: list[NewsItem]) -> None:
        """批量存储新闻到 Redis"""
        try:
            redis = await self._get_redis()
            async with redis.pipeline(transaction=False) as pipe:
                for news in news_list:
                    pipe.hset(news.redis_key, mapping=news.to_redis_hash())
                    pipe.expire(news.redis_key, CACHE_TTL)
                await pipe.execute()
            logger.debug("新闻已存储到 Redis", count=len(news_list))
        except Exception as e:
            logger.warning("存储新闻到 Redis 失败", error=str(e))

    async def fetch_by_symbol(
        self,
        symbol: str,
        api_key: str | None = None,
        limit: int = 10,
        store_redis: bool = True,
    ) -> list[NewsItem]:
        """
        获取指定币种的相关新闻。

        参数:
            symbol: 币种，如 "BTC", "ETH"
            api_key: CryptoPanic API Key
            limit: 返回条数
            store_redis: 是否存储到 Redis

        返回:
            相关的 NewsItem 列表
        """
        return await self.fetch_latest(
            api_key=api_key,
            currencies=symbol,
            limit=limit,
            store_redis=store_redis,
        )
