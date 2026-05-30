"""
模块名称: cache_manager.py
所属层级: 指标计算层 (Indicators)
输入来源: 由 indicator-worker 调用，传入指标计算结果和参数
输出去向: Redis 缓存（仅慢周期），key 格式 indicators:{symbol}:{timeframe}:{ts}
关键依赖: redis.asyncio, structlog, yaml

修订记录:
- v1.0: 初始实现，慢周期预计算缓存 + TTL 管理
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# 慢周期及其对应 TTL（秒）
# 1d=86400s, 4h=14400s, 1h=3600s
SLOW_TIMEFRAMES: dict[str, int] = {
    "1d": 86400,
    "4h": 14400,
    "1h": 3600,
}

# 快周期列表（不缓存，实时计算）
FAST_TIMEFRAMES: set[str] = {"15m", "5m", "1m"}

# Redis key 前缀
_KEY_PREFIX = "indicators"


@dataclass
class CacheEntry:
    """缓存条目结构"""
    symbol: str
    timeframe: str
    ts: int
    indicators: dict[str, float | None]


def is_slow_timeframe(timeframe: str) -> bool:
    """
    判断是否属于慢周期（需要缓存）。

    参数:
        timeframe: 时间周期，如 "1d", "4h", "1h", "15m", "5m", "1m"

    返回:
        True 表示需要缓存，False 表示实时计算
    """
    return timeframe in SLOW_TIMEFRAMES


def is_fast_timeframe(timeframe: str) -> bool:
    """
    判断是否属于快周期（不缓存）。

    参数:
        timeframe: 时间周期

    返回:
        True 表示不缓存，实时计算
    """
    return timeframe in FAST_TIMEFRAMES


def get_ttl(timeframe: str) -> int:
    """
    获取指定时间周期的缓存 TTL（秒）。

    参数:
        timeframe: 时间周期

    返回:
        TTL 秒数。若周期未知，默认返回 3600（1小时）。
    """
    return SLOW_TIMEFRAMES.get(timeframe, 3600)


def build_cache_key(symbol: str, timeframe: str, ts: int) -> str:
    """
    构建 Redis 缓存 key。

    格式: indicators:{symbol}:{timeframe}:{ts}

    参数:
        symbol: 交易对，如 "BTCUSDT"
        timeframe: 时间周期，如 "1h"
        ts: K 线开盘时间戳（毫秒）

    返回:
        完整的 Redis key 字符串
    """
    return f"{_KEY_PREFIX}:{symbol}:{timeframe}:{ts}"


def serialize_entry(entry: CacheEntry) -> str:
    """
    将 CacheEntry 序列化为 JSON 字符串（用于 Redis 存储）。

    参数:
        entry: 缓存的指标条目

    返回:
        JSON 字符串
    """
    return json.dumps({
        "symbol": entry.symbol,
        "timeframe": entry.timeframe,
        "ts": entry.ts,
        "indicators": entry.indicators,
    })


def deserialize_entry(data: str) -> CacheEntry | None:
    """
    从 JSON 字符串反序列化为 CacheEntry。

    参数:
        data: Redis 中读取的 JSON 字符串

    返回:
        解析成功返回 CacheEntry，失败返回 None
    """
    try:
        obj = json.loads(data)
        return CacheEntry(
            symbol=obj["symbol"],
            timeframe=obj["timeframe"],
            ts=obj["ts"],
            indicators=obj["indicators"],
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.error("反序列化缓存条目失败", error=str(e), data_preview=str(data)[:200])
        return None


async def cache_indicators(
    redis_client: Any,
    symbol: str,
    timeframe: str,
    ts: int,
    indicators: dict[str, float | None],
) -> bool:
    """
    将指标结果缓存到 Redis（仅慢周期）。

    快周期（15m/5m/1m）调用此函数不会执行任何操作，直接返回 True。

    参数:
        redis_client: async Redis 客户端实例
        symbol: 交易对
        timeframe: 时间周期
        ts: K 线开盘时间戳（毫秒）
        indicators: 指标字典，如 {"RSI_14": 58.3, "ATR_14": 380.2}

    返回:
        True 表示成功（或跳过），False 表示失败
    """
    # 快周期跳过缓存
    if is_fast_timeframe(timeframe):
        return True

    if not indicators:
        logger.warning("缓存跳过：空指标字典", symbol=symbol, timeframe=timeframe)
        return False

    ttl = get_ttl(timeframe)
    key = build_cache_key(symbol, timeframe, ts)
    entry = CacheEntry(symbol=symbol, timeframe=timeframe, ts=ts, indicators=indicators)

    try:
        data = serialize_entry(entry)
        await redis_client.setex(key, ttl, data)
        logger.debug(
            "指标已缓存",
            symbol=symbol,
            timeframe=timeframe,
            ts=ts,
            ttl=ttl,
            indicator_count=len(indicators),
        )
        return True
    except Exception as e:
        logger.error("缓存指标到 Redis 失败", error=str(e), key=key)
        return False


async def get_cached_indicators(
    redis_client: Any,
    symbol: str,
    timeframe: str,
    ts: int,
) -> CacheEntry | None:
    """
    从 Redis 读取缓存的指标结果。

    参数:
        redis_client: async Redis 客户端实例
        symbol: 交易对
        timeframe: 时间周期
        ts: K 线开盘时间戳（毫秒）

    返回:
        命中缓存返回 CacheEntry，未命中或失败返回 None
    """
    key = build_cache_key(symbol, timeframe, ts)

    try:
        data = await redis_client.get(key)
        if data is None:
            logger.debug("缓存未命中", key=key)
            return None

        entry = deserialize_entry(data)
        if entry is not None:
            logger.debug("缓存命中", key=key)
        return entry
    except Exception as e:
        logger.error("从 Redis 读取缓存失败", error=str(e), key=key)
        return None


async def invalidate_cache(
    redis_client: Any,
    symbol: str,
    timeframe: str,
    ts: int | None = None,
) -> bool:
    """
    使指定缓存失效（删除 key）。

    参数:
        redis_client: async Redis 客户端实例
        symbol: 交易对
        timeframe: 时间周期
        ts: 可选，指定时间戳则只删除该条；为 None 时使用模式匹配删除全部

    返回:
        True 表示操作成功，False 表示失败
    """
    try:
        if ts is not None:
            key = build_cache_key(symbol, timeframe, ts)
            await redis_client.delete(key)
            logger.debug("缓存已删除", key=key)
        else:
            # 删除该交易对+周期的所有缓存（使用 scan 匹配）
            pattern = f"{_KEY_PREFIX}:{symbol}:{timeframe}:*"
            cursor = 0
            deleted_count = 0
            while True:
                cursor, keys = await redis_client.scan(cursor=cursor, match=pattern, count=100)
                if keys:
                    await redis_client.delete(*keys)
                    deleted_count += len(keys)
                if cursor == 0:
                    break
            logger.debug("批量缓存已删除", pattern=pattern, count=deleted_count)
        return True
    except Exception as e:
        logger.error("使缓存失效失败", error=str(e), symbol=symbol, timeframe=timeframe)
        return False


def get_max_warmup(config: dict[str, Any] | None = None) -> int:
    """
    计算所有指标所需的最大 warmup 行数。
    用于上层 worker 在计算前确保有足够数据。

    参数:
        config: 完整的 indicators.yml 配置 dict

    返回:
        所需的最大历史 K 线根数
    """
    if config is None:
        from .momentum import load_momentum_params
        from .volatility import load_volatility_params
        from .volume import load_volume_params
        from .trend import get_required_warmup as trend_warmup

        trend_req = trend_warmup()
        mom_cfg = load_momentum_params()
        vol_cfg = load_volatility_params()
        vol_cfg = load_volume_params()

        warmups = [
            trend_req,
            mom_cfg.get("rsi_period", 14),
            mom_cfg.get("cci_period", 20),
            mom_cfg.get("stoch", {}).get("k", 14),
            vol_cfg.get("atr_period", 14),
            vol_cfg.get("stddev_period", 20),
            vol_cfg.get("bbands", {}).get("period", 20),
        ]
    else:
        warmups = [
            max(config.get("trend", {}).get("ema_periods", [200])),
            config.get("momentum", {}).get("rsi_period", 14),
            config.get("momentum", {}).get("cci_period", 20),
            config.get("momentum", {}).get("stoch", {}).get("k", 14),
            config.get("volatility", {}).get("atr_period", 14),
            config.get("volatility", {}).get("stddev_period", 20),
            config.get("volatility", {}).get("bbands", {}).get("period", 20),
        ]

    return max(warmups) + 1  # +1 作为安全缓冲


# 公开 API
__all__ = [
    "SLOW_TIMEFRAMES",
    "FAST_TIMEFRAMES",
    "CacheEntry",
    "is_slow_timeframe",
    "is_fast_timeframe",
    "get_ttl",
    "build_cache_key",
    "serialize_entry",
    "deserialize_entry",
    "cache_indicators",
    "get_cached_indicators",
    "invalidate_cache",
    "get_max_warmup",
]
