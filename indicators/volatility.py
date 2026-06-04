"""
模块名称: volatility.py
所属层级: 指标计算层 (Indicators)
输入来源: OHLCV DataFrame（列: open, high, low, close, volume）
输出去向: 追加波动率指标列的 DataFrame（NaN 保留，不丢弃行）
关键依赖: pandas, numpy, structlog, yaml
（无外部第三方指标库依赖，全部使用 pandas/numpy 原生实现）

修订记录:
- v2.0: 移除 pandas_ta 依赖，全部改用 pandas/numpy 原生实现
        ATR → TR = max(H-L, |H-prevC|, |L-prevC|), then EMA
        STDDEV → rolling.std(ddof=0)
        BBANDS → SMA ± std × multiplier
- v1.0: 初始实现，ATR(14) + STDDEV(20) + BBANDS(20,2)
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np
import structlog
import yaml

logger = structlog.get_logger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent / "config"
CONFIG_PATH = _CONFIG_DIR / "indicators.yml"


@lru_cache(maxsize=1)
def load_volatility_params(config_path: str | Path | None = None) -> dict[str, Any]:
    """
    从 config/indicators.yml 的 volatility 段读取参数。

    返回结构:
    {
        "atr_period": 14,
        "stddev_period": 20,
        "bbands": {"period": 20, "std": 2}
    }
    """
    cfg_path = Path(config_path) if config_path else CONFIG_PATH

    if not cfg_path.exists():
        logger.warning("配置文件未找到，使用默认参数", path=str(cfg_path))
        return {
            "atr_period": 14,
            "stddev_period": 20,
            "bbands": {"period": 20, "std": 2},
        }

    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    vol_cfg = cfg.get("volatility", {})
    defaults = {
        "atr_period": 14,
        "stddev_period": 20,
        "bbands": {"period": 20, "std": 2},
    }

    # bbands 子字段合并
    if "bbands" in vol_cfg and isinstance(vol_cfg["bbands"], dict):
        bbands_default = defaults["bbands"]
        vol_cfg["bbands"] = {**bbands_default, **vol_cfg["bbands"]}

    for key in defaults:
        if key not in vol_cfg:
            logger.warning("配置文件缺少 volatility.%s，使用默认值", key, key=key, default=defaults[key])

    return {**defaults, **vol_cfg}


def compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """
    计算 ATR（平均真实波幅）。

    参数:
        df: 含 high, low, close 列的 DataFrame
        period: ATR 回溯周期（标准 14）

    返回:
        名为 ATR_{period} 的 Series
    """
    required = ["high", "low", "close"]
    for col in required:
        if col not in df.columns:
            logger.warning("DataFrame 缺少 '%s' 列", col, column=col)
            return pd.Series(index=df.index, dtype=float, name=f"ATR_{period}")

        """
    纯 pandas 实现 ATR（平均真实波幅）。

    算法:
        TR = max(high - low, |high - prev_close|, |low - prev_close|)
        ATR = EMA(TR, period)
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()

    # 三个 TR 中的最大值
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # ATR = EMA of True Range
    atr = true_range.ewm(span=period, adjust=False).mean()
    atr.name = f"ATR_{period}"
    return atr


def compute_stddev(df: pd.DataFrame, period: int) -> pd.Series:
    """
    计算 STDDEV（标准差）。

    参数:
        df: 含 close 列的 DataFrame
        period: 标准差回溯周期（标准 20）

    返回:
        名为 STDDEV_{period} 的 Series
    """
    if "close" not in df.columns:
        logger.warning("DataFrame 缺少 'close' 列")
        return pd.Series(index=df.index, dtype=float, name=f"STDDEV_{period}")

    stddev = df["close"].rolling(window=period, min_periods=period).std(ddof=0)
    stddev.name = f"STDDEV_{period}"
    return stddev


def compute_bbands(df: pd.DataFrame, period: int, std: int | float) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    计算 BBANDS（布林带）。

    参数:
        df: 含 close 列的 DataFrame
        period: 布林带回溯周期（标准 20）
        std: 标准差倍数（标准 2）

    返回:
        (BBANDS_upper_{period}_{std}, BBANDS_mid_{period}_{std}, BBANDS_lower_{period}_{std}) 的 tuple
    """
    if "close" not in df.columns:
        logger.warning("DataFrame 缺少 'close' 列")
        empty = pd.Series(index=df.index, dtype=float, name=f"BBANDS_upper_{period}_{std}")
        return empty, empty.copy(), empty.copy()

        """
    纯 pandas 实现 BBANDS（布林带）。

    算法:
        mid = SMA(close, period)
        std = 标准差(close, period)
        upper = mid + std × multiplier
        lower = mid - std × multiplier
    """
    close = df["close"]
    mid = close.rolling(window=period, min_periods=period).mean()
    std_series = close.rolling(window=period, min_periods=period).std(ddof=0)

    upper_band = mid + std_series * std
    lower_band = mid - std_series * std

    upper = upper_band.rename(f"BBANDS_upper_{period}_{std}")
    mid_renamed = mid.rename(f"BBANDS_mid_{period}_{std}")
    lower = lower_band.rename(f"BBANDS_lower_{period}_{std}")

    return upper, mid_renamed, lower


def compute_volatility(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """
    计算所有波动率指标（ATR + STDDEV + BBANDS），追加到 DataFrame。

    参数:
        df: OHLCV DataFrame（列: open, high, low, close, volume）
        cfg: indicators.yml 的 volatility 段配置。为 None 时自动读取。

    返回:
        追加了以下列的 DataFrame:
        - ATR_{period}
        - STDDEV_{period}
        - BBANDS_upper_{period}_{std}
        - BBANDS_mid_{period}_{std}
        - BBANDS_lower_{period}_{std}

    注意:
        - 不丢弃任何行，数据不足时对应位置为 NaN
        - 纯函数，不修改输入 df
    """
    df = df.copy()

    if cfg is None:
        cfg = load_volatility_params()

    # 验证必要列
    for col in ["high", "low", "close"]:
        if col not in df.columns:
            logger.warning("DataFrame 缺少必要列 '%s'", col, column=col)
            return df

    if not pd.api.types.is_numeric_dtype(df["close"]):
        logger.warning("'close' 列必须为数值类型")
        return df

    try:
        # ATR
        df[f"ATR_{cfg['atr_period']}"] = compute_atr(df, cfg["atr_period"])

        # STDDEV
        df[f"STDDEV_{cfg['stddev_period']}"] = compute_stddev(df, cfg["stddev_period"])

        # BBANDS
        upper, mid, lower = compute_bbands(
            df,
            period=cfg["bbands"]["period"],
            std=cfg["bbands"]["std"],
        )
        df[upper.name] = upper
        df[mid.name] = mid
        df[lower.name] = lower

    except Exception as e:
        logger.error("波动率指标计算异常", error=str(e))

    return df
