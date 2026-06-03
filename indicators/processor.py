"""
模块名称: processor.py
所属层级: 指标计算层 (Indicators)
输入来源: raw_kline Stream（消息格式见 STREAM_SCHEMA.md）
输出去向: indicators Stream（包含各周期指标计算结果）
关键依赖: trend, momentum, volatility, volume, timeseries, math_factors

raw_kline → 指标计算管道。

消费 raw_kline Stream 中的 K 线数据，
调用各指标模块计算，合并结果后发布到 indicators Stream。

注意：单根 K 线无法计算指标，模块内部维护滑动窗口缓存。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd
import structlog

from indicators.trend import calculate_trend
from indicators.momentum import compute_momentum
from indicators.volatility import compute_volatility
from indicators.volume import compute_volume
from indicators.timeseries import compute_timeseries
from indicators.math_factors import compute_math_factors

logger = structlog.get_logger(__name__)

# ─── 滑动窗口缓存 ─────────────────────────────────────────────
# 按 (symbol, timeframe) 分组，缓存最近 N 根 K 线
# 指标计算需要足够的历史数据（warmup）
_CACHE_SIZE = 300
_kline_cache: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(
    lambda: []
)


async def process_raw_kline(message: dict[str, Any]) -> dict[str, Any] | None:
    """
    处理单条 raw_kline 消息。

    1. 加入滑动窗口缓存
    2. 缓存长度达到 warmup 要求时，计算指标
    3. 发布合并后的指标结果

    参数:
        message: raw_kline Stream 消息
            - symbol, timeframe, ts, open, high, low, close, volume, ...

    返回:
        indicators Stream 消息（含所有计算好的指标），
        或 None（缓存不足，跳过本条）
    """
    symbol: str = message.get("symbol", "")
    timeframe: str = message.get("timeframe", "")
    key = (symbol, timeframe)

    # ─── 加入缓存 ─────────────────────────────────────────
    cache = _kline_cache[key]
    cache.append(message)

    # 修剪缓存，防止内存泄漏
    if len(cache) > _CACHE_SIZE:
        cache.pop(0)

    # ─── 等待足够数据预热 ─────────────────────────────────
    # 最少需要 200 根 K 线（覆盖 max(EMA_200, MACD 慢周期 26) ≈ 200）
    MIN_WARMPUP = 200
    if len(cache) < MIN_WARMPUP:
        logger.debug(
            "缓存预热中",
            symbol=symbol,
            timeframe=timeframe,
            cached=len(cache),
            required=MIN_WARMPUP,
        )
        return None

    # ─── 构建 DataFrame ────────────────────────────────────
    df = pd.DataFrame(cache)
    # 确保数值列类型正确
    for col in ("open", "high", "low", "close", "volume", "quote_volume",
                "taker_buy_volume", "taker_buy_quote"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # ─── 计算各分类指标 ────────────────────────────────────
    try:
        # 先计算所有不丢弃行的指标（在全量 DataFrame df 上操作）
        df_out = compute_momentum(df)
        df_out = compute_volatility(df_out)
        df_out = compute_volume(df_out)
        df_out = compute_timeseries(df_out)
        df_out = compute_math_factors(df_out)

        # 最后计算趋势指标（会丢弃 required_warmup 行）
        df_out = calculate_trend(df_out)
        if df_out.empty:
            logger.debug("趋势指标计算后无数据（预热行被丢弃）", symbol=symbol)
            return None

    except Exception as exc:
        logger.error("指标计算失败", symbol=symbol, error=str(exc))
        return None

    # 取最新一行
    latest = df_out.iloc[-1]

    # 构建指标字典（仅数值列，跳过 symbol/timeframe/ts 等元数据列）
    indicators: dict[str, float] = {}
    for col in df_out.columns:
        val = latest[col]
        if pd.notna(val) and isinstance(val, (int, float, np.floating, np.integer)):
            indicators[col] = float(val)

    # ─── 合并结果 ──────────────────────────────────────────
    result: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "ts": message.get("ts", 0),
        "close": float(latest.get("close", 0)),
        "volume": float(latest.get("volume", 0)),
        "indicators": indicators,
        "cached_kline_count": len(cache),
    }

    logger.info(
        "指标计算完成",
        symbol=symbol,
        timeframe=timeframe,
        indicator_count=len(indicators),
        close=result["close"],
    )

    return result


def clear_cache(symbol: str | None = None, timeframe: str | None = None) -> int:
    """
    清空指定缓存（用于测试 / 重连时重置）。

    参数:
        symbol: 交易对，None 表示所有
        timeframe: 周期，None 表示所有

    返回:
        被清空的缓存条目数
    """
    global _kline_cache
    cleared = 0
    if symbol is None and timeframe is None:
        cleared = sum(len(v) for v in _kline_cache.values())
        _kline_cache.clear()
    else:
        keys_to_delete = [
            k for k in _kline_cache
            if (symbol is None or k[0] == symbol)
            and (timeframe is None or k[1] == timeframe)
        ]
        for k in keys_to_delete:
            cleared += len(_kline_cache.pop(k, []))
    logger.info("缓存已清空", symbol=symbol, timeframe=timeframe, cleared=cleared)
    return cleared


__all__ = ["process_raw_kline", "clear_cache"]
