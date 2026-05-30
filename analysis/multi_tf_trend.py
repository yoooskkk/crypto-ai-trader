"""
模块名称: multi_tf_trend.py
所属层级: 分析层 (Analysis)
输入来源: indicators Stream + regime_signal Stream（周期分组后的指标数据）
输出去向: 返回值 dict 供 prompt_builder 和 plan_generator 消费
关键依赖: structlog

多周期趋势共识分析。
根据 PRIMARY / CONFIRM / FAST 时间框架划分，计算方向共识和强度。
防漂移规则：FAST 周期只用于入场时机，不参与方向判断。

修订记录:
- v1.0: 初始实现，多周期共识 + 防漂移规则
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ─── 周期常量（禁止修改） ──────────────────────────────────────

PRIMARY: str = "1h"
CONFIRM: list[str] = ["4h", "1d"]
FAST: list[str] = ["5m", "15m"]
ALL_TF: list[str] = [PRIMARY] + CONFIRM + FAST

# ─── 方向类型 ─────────────────────────────────────────────────

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_FLAT = "FLAT"
STRENGTH_STRONG = "STRONG"
STRENGTH_WEAK = "WEAK"

VALID_DIRECTIONS = {DIRECTION_LONG, DIRECTION_SHORT, DIRECTION_FLAT}


# ─── 指标方向推断 ─────────────────────────────────────────────

def infer_trend_direction(
    indicators: dict[str, float],
    regime: str = "UNKNOWN",
) -> str:
    """
    根据单个时间框架的指标值推断方向。

    推断逻辑（与 ARCH.md 第7节制度联动保持一致）:
      - TRENDING  → EMA 排列 + MACD 为主
      - RANGING   → RSI/STOCH 为主，关闭趋势信号
      - HIGH_VOLATILITY → 谨慎，以 ATR/BB 为参考
      - UNKNOWN   → 极保守

    参数:
        indicators: {指标名: 值} 字典
        regime: 当前市场制度

    返回:
        "LONG" | "SHORT" | "FLAT"
    """
    bullish_count = 0
    bearish_count = 0

    # --- EMA 排列信号 ---
    ema_periods = [9, 21, 55, 200]
    ema_values = {}
    for p in ema_periods:
        key = f"EMA_{p}"
        val = indicators.get(key)
        if val is not None and not _is_nan(val):
            ema_values[p] = val

    if len(ema_values) >= 3:
        sorted_periods = sorted(ema_values.keys())
        # 多头排列：短周期 EMA > 长周期 EMA
        if all(ema_values[sorted_periods[i]] > ema_values[sorted_periods[i + 1]]
               for i in range(len(sorted_periods) - 1)):
            bullish_count += 2
        # 空头排列：短周期 EMA < 长周期 EMA
        elif all(ema_values[sorted_periods[i]] < ema_values[sorted_periods[i + 1]]
                 for i in range(len(sorted_periods) - 1)):
            bearish_count += 2

    # --- SMA 信号 ---
    sma_val = indicators.get("SMA_20")
    close_val = indicators.get("close")
    if close_val is not None and sma_val is not None and not _is_nan(close_val) and not _is_nan(sma_val):
        if close_val > sma_val:
            bullish_count += 1
        else:
            bearish_count += 1

    # --- MACD 信号 ---
    macd_hist = indicators.get("MACD_hist")
    if macd_hist is not None and not _is_nan(macd_hist):
        if macd_hist > 0:
            bullish_count += 1
        else:
            bearish_count += 1

    # --- RSI 信号 ---
    rsi = indicators.get("RSI_14")
    if rsi is not None and not _is_nan(rsi):
        if 30 <= rsi <= 40:
            bullish_count += 1
        elif rsi > 70:
            bearish_count += 1
        elif 40 < rsi < 60:
            pass
        elif rsi <= 30:
            bullish_count += 2
        elif rsi >= 80:
            bearish_count += 2

    # --- ADX 强度验证 ---
    adx = indicators.get("ADX_14")
    if adx is not None and not _is_nan(adx) and adx < 20 and regime == "RANGING":
        return DIRECTION_FLAT

    # --- VWAP 信号 ---
    vwap = indicators.get("VWAP")
    if close_val is not None and vwap is not None and not _is_nan(close_val) and not _is_nan(vwap):
        if close_val > vwap * 1.005:
            bullish_count += 1
        elif close_val < vwap * 0.995:
            bearish_count += 1

    # --- 决定方向 ---
    if regime == "TRENDING":
        threshold = 2
    elif regime == "RANGING":
        threshold = 1
    elif regime == "HIGH_VOLATILITY":
        threshold = 3
    else:
        threshold = 3

    if bullish_count >= threshold and bullish_count > bearish_count:
        return DIRECTION_LONG
    elif bearish_count >= threshold and bearish_count > bullish_count:
        return DIRECTION_SHORT
    else:
        return DIRECTION_FLAT


# ─── 多周期共识 ───────────────────────────────────────────────

def compute_tf_trends(
    tf_indicators: dict[str, dict[str, float]],
    regime: str = "UNKNOWN",
) -> dict[str, dict[str, Any]]:
    """
    计算每个时间框架的方向。

    参数:
        tf_indicators: {timeframe: {indicator_name: value}}
        regime: 当前市场制度

    返回:
        {
            "1h":  {"direction": "LONG", "indicators": {...}},
            "4h":  {"direction": "SHORT", ...},
            ...
        }
    """
    trends: dict[str, dict[str, Any]] = {}
    for tf, indics in tf_indicators.items():
        direction = infer_trend_direction(indics, regime)
        trends[tf] = {
            "direction": direction,
            "indicators": indics,
        }
    return trends


def get_consensus(trends: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """
    计算多周期共识。

    规则:
      - PRIMARY (1h) 方向 + 至少 1 个 CONFIRM (4h/1d) 同向 = STRONG
      - 仅 PRIMARY 有方向（CONFIRM 为 FLAT 或缺失） = WEAK
      - PRIMARY 为 FLAT = FLAT
      - FAST 周期不参与方向判断

    参数:
        trends: compute_tf_trends() 的输出

    返回:
        (direction, strength)
    """
    primary = trends.get(PRIMARY, {})
    primary_dir = primary.get("direction", DIRECTION_FLAT)

    if primary_dir == DIRECTION_FLAT:
        return (DIRECTION_FLAT, STRENGTH_WEAK)

    confirm_count = 0
    for tf in CONFIRM:
        tf_data = trends.get(tf, {})
        if tf_data.get("direction") == primary_dir:
            confirm_count += 1

    if confirm_count >= 1:
        return (primary_dir, STRENGTH_STRONG)
    else:
        return (primary_dir, STRENGTH_WEAK)


def get_fast_entry_bias(trends: dict[str, dict[str, Any]], consensus_dir: str) -> str | None:
    """
    获取 FAST 周期入场偏向（仅用于入场时机微调，不改变方向）。

    参数:
        trends: compute_tf_trends() 的输出
        consensus_dir: get_consensus() 返回的方向

    返回:
        "LONG" | "SHORT" | None
    """
    if consensus_dir == DIRECTION_FLAT:
        return None

    fast_align = 0
    for tf in FAST:
        tf_data = trends.get(tf, {})
        if tf_data.get("direction") == consensus_dir:
            fast_align += 1

    if fast_align >= len(FAST) / 2:
        return consensus_dir

    return None


def build_trend_summary(
    tf_indicators: dict[str, dict[str, float]],
    regime: str = "UNKNOWN",
) -> dict[str, Any]:
    """
    完整的多周期趋势分析入口。

    参数:
        tf_indicators: {timeframe: {indicator_name: value}}
        regime: 当前市场制度

    返回:
        {
            "consensus": {"direction": "LONG", "strength": "STRONG"},
            "primary": "1h",
            "trends": {...},
            "entry_bias": "LONG",
            "regime": "TRENDING",
        }
    """
    trends = compute_tf_trends(tf_indicators, regime)
    direction, strength = get_consensus(trends)
    entry_bias = get_fast_entry_bias(trends, direction)

    summary = {
        "consensus": {
            "direction": direction,
            "strength": strength,
        },
        "primary": PRIMARY,
        "confirm_timeframes": CONFIRM,
        "fast_timeframes": FAST,
        "trends": trends,
        "entry_bias": entry_bias,
        "regime": regime,
    }

    logger.debug(
        "多周期趋势分析完成",
        direction=direction,
        strength=strength,
        entry_bias=entry_bias,
        primary_tf=PRIMARY,
    )

    return summary


# ─── 辅助函数 ─────────────────────────────────────────────────

def _is_nan(val: float) -> bool:
    import math
    return math.isnan(val)


__all__ = [
    "PRIMARY", "CONFIRM", "FAST", "ALL_TF",
    "DIRECTION_LONG", "DIRECTION_SHORT", "DIRECTION_FLAT",
    "STRENGTH_STRONG", "STRENGTH_WEAK",
    "infer_trend_direction",
    "compute_tf_trends",
    "get_consensus",
    "get_fast_entry_bias",
    "build_trend_summary",
]
