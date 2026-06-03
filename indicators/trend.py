"""
模块名称: trend.py
所属层级: 指标计算层 (Indicators)
输入来源: 经过预热的 OHLCV DataFrame（至少需包含 required_warmup 行数据，详见说明）
输出去向: 在 DataFrame 上追加指标列，并丢弃前 required_warmup 行（避免 NaN）
关键依赖: pandas, yaml, functools, os, pathlib
（无外部第三方指标库依赖，全部使用 pandas/numpy 原生实现）

修订记录:
- v2.0: 移除 pandas_ta 依赖，全部改用 pandas/numpy 原生实现
        EMA → series.ewm(span).mean()
        SMA → series.rolling(window).mean()
        MACD → ema_fast - ema_slow + signal ema
- v1.2 (2025-03-13): [P0] 添加 close 列校验；YAML schema 校验；函数纯化（copy）。
                      [P1] MACD 列名标准化；丢弃预热行（NaN 处理）。
"""
from __future__ import annotations

import structlog
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np
import yaml

logger = structlog.get_logger(__name__)

# 配置文件路径：优先使用环境变量 CONFIG_DIR，否则使用项目根目录下的 config
_CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", Path(__file__).parent.parent / "config"))
CONFIG_PATH = _CONFIG_DIR / "indicators.yml"


@lru_cache(maxsize=1)
def load_trend_params() -> dict[str, Any]:
    """
    从 config/indicators.yml 读取趋势指标参数，结果会被缓存。
    返回字典结构如:
    {
        "ema": {"periods": [9, 21, 55, 200]},
        "macd": {"fast": 12, "slow": 26, "signal": 9}
    }
    若配置文件缺失或格式不正确，将抛出 ValueError 或使用默认值。
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning("配置文件 %s 未找到，使用默认参数", CONFIG_PATH)
        return {
            "ema": {"periods": [9, 21, 55, 200]},
            "macd": {"fast": 12, "slow": 26, "signal": 9},
        }

    trend_config = config.get("trend", {})
    ema_cfg = trend_config.get("ema", {"periods": [9, 21, 55, 200]})
    macd_cfg = trend_config.get("macd", {"fast": 12, "slow": 26, "signal": 9})

    # YAML schema 校验 (P0)
    if not isinstance(ema_cfg.get("periods"), list):
        raise ValueError("indicators.yml trend.ema.periods 必须为列表")
    for p in ema_cfg["periods"]:
        if not isinstance(p, int) or p <= 0:
            raise ValueError(f"indicators.yml trend.ema.periods 包含非法值: {p}")

    if not isinstance(macd_cfg.get("fast"), int) or macd_cfg["fast"] <= 0:
        raise ValueError("indicators.yml trend.macd.fast 必须为正整数")
    if not isinstance(macd_cfg.get("slow"), int) or macd_cfg["slow"] <= 0:
        raise ValueError("indicators.yml trend.macd.slow 必须为正整数")
    if not isinstance(macd_cfg.get("signal"), int) or macd_cfg["signal"] <= 0:
        raise ValueError("indicators.yml trend.macd.signal 必须为正整数")

    return {
        "ema": ema_cfg,
        "macd": macd_cfg,
    }


def get_required_warmup() -> int:
    """
    返回为确保指标有效性所需的最少历史 K 线根数（严格最小）。
    计算公式：max(所有 EMA 周期的最大值, MACD 慢周期)
    注意：此值不包含额外缓冲，调用方可自行增加（例如 +1 或 ×2）。
    """
    params = load_trend_params()
    max_ema = max(params["ema"]["periods"])
    max_macd_slow = params["macd"]["slow"]
    return max(max_ema, max_macd_slow)


def _validate_close_column(df: pd.DataFrame) -> None:
    """
    校验 DataFrame 是否包含有效的 'close' 列 (P0)。
    若缺失或非数值类型，则抛出 ValueError。
    """
    if "close" not in df.columns:
        raise ValueError("DataFrame 必须包含 'close' 列")
    if not pd.api.types.is_numeric_dtype(df["close"]):
        raise ValueError("'close' 列必须为数值类型")


def _ema(series: pd.Series, period: int) -> pd.Series:
    """纯 pandas 实现 EMA，无外部依赖。"""
    return series.ewm(span=period, adjust=False).mean()


def _sma(series: pd.Series, period: int) -> pd.Series:
    """纯 pandas 实现 SMA（简单移动平均）。"""
    return series.rolling(window=period, min_periods=period).mean()


def add_ema(df: pd.DataFrame, periods: list[int]) -> pd.DataFrame:
    """为 DataFrame 添加指定周期的 EMA 列。"""
    close = df["close"]
    for p in periods:
        col_name = f"EMA_{p}"
        if col_name not in df.columns:
            df[col_name] = _ema(close, p)
            logger.debug("添加趋势指标: %s (周期=%d)", col_name, p)
        else:
            logger.debug("趋势指标 %s 已存在，跳过计算", col_name)
    return df


def add_sma(df: pd.DataFrame, periods: list[int]) -> pd.DataFrame:
    """为 DataFrame 添加指定周期的 SMA 列（下游需要时调用）。"""
    close = df["close"]
    for p in periods:
        col_name = f"SMA_{p}"
        if col_name not in df.columns:
            df[col_name] = _sma(close, p)
            logger.debug("添加趋势指标: %s (周期=%d)", col_name, p)
    return df


def add_macd(df: pd.DataFrame, fast: int, slow: int, signal: int) -> pd.DataFrame:
    """
    为 DataFrame 添加 MACD 指标（纯 pandas 实现）。
    生成列后，重命名为标准化名称: MACD, MACDh, MACDs (P1)。

    算法:
        MACD线 = EMA(fast) - EMA(slow)
        信号线 = EMA(MACD线, signal)
        柱状图 = MACD线 - 信号线
    """
    macd_col = "MACD"
    hist_col = "MACDh"
    signal_col = "MACDs"

    if macd_col not in df.columns:
        close = df["close"]
        ema_fast = _ema(close, fast)
        ema_slow = _ema(close, slow)
        macd_line = ema_fast - ema_slow
        signal_line = _ema(macd_line, signal)
        histogram = macd_line - signal_line

        df[macd_col] = macd_line
        df[signal_col] = signal_line
        df[hist_col] = histogram

        logger.debug("添加趋势指标: MACD (fast=%d, slow=%d, signal=%d)", fast, slow, signal)
    else:
        logger.debug("MACD 指标列已存在，跳过计算")

    return df


def calculate_trend(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算所有趋势指标（EMA + MACD），参数完全由 config/indicators.yml 驱动。
    返回增加了指标列的 DataFrame，且已丢弃前 required_warmup 行（即所有行均无 NaN）。

    P0 修复:
      - 纯函数：开头 copy()，不修改入参。
      - close 列校验。
      - YAML schema 在 load_trend_params 中校验。

    P1 修复:
      - MACD 列名标准化。
      - NaN 处理：丢弃预热行。

    注意：返回的行数比输入少 required_warmup 行。
    """
    # P0: 纯函数，不修改原始数据
    df = df.copy()

    # P0: close 列校验
    _validate_close_column(df)

    params = load_trend_params()
    warmup = get_required_warmup()

    # 预热检查与警告
    if len(df) < warmup:
        logger.warning(
            "DataFrame 行数 (%d) 不足 required_warmup (%d)，"
            "将在计算后丢弃无效行，结果可能为空。",
            len(df), warmup,
        )

    # EMA
    df = add_ema(df, params["ema"]["periods"])

    # MACD（已包含列名标准化）
    df = add_macd(df, params["macd"]["fast"], params["macd"]["slow"], params["macd"]["signal"])

    # P1: NaN 处理 - 丢弃前 warmup 行，保证剩余行所有指标均有效
    if 0 < warmup <= len(df):
        df = df.iloc[warmup:].reset_index(drop=True)
        logger.debug("已丢弃前 %d 行预热数据，剩余 %d 行", warmup, len(df))
    elif warmup > len(df):
        # 数据不足以丢弃，返回空 DataFrame（列结构保留）
        df = df.iloc[:0].copy()

    return df