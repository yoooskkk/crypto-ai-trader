"""
模块名称: timeseries.py
所属层级: 指标计算层 (Indicators)
输入来源: OHLCV DataFrame（列: open, high, low, close, volume）
输出去向: 追加时序操作列的 DataFrame（NaN 保留，不丢弃行）
关键依赖: pandas, numpy, structlog, yaml
（无外部第三方指标库依赖，全部使用 pandas/numpy 原生实现）

修订记录:
- v1.0: 初始实现，DELAY + DELTA + TS_MAX + TS_MIN + TS_RANK + TS_ZSCORE + CORR（已是纯 pandas 实现）
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

# 默认参数（config 中无 timeseries 段时的后备值）
_DEFAULT_TS_PARAMS: dict[str, Any] = {
    "delay_period": 1,
    "delta_period": 1,
    "ts_max_period": 20,
    "ts_min_period": 20,
    "ts_rank_period": 20,
    "ts_zscore_period": 20,
    "corr_period": 20,
}


@lru_cache(maxsize=1)
def load_timeseries_params(config_path: str | Path | None = None) -> dict[str, Any]:
    """
    从 config/indicators.yml 的 timeseries 段读取参数。
    若配置文件中无 timeseries 段，使用默认值。

    返回结构:
    {
        "delay_period": 1,
        "delta_period": 1,
        "ts_max_period": 20,
        "ts_min_period": 20,
        "ts_rank_period": 20,
        "ts_zscore_period": 20,
        "corr_period": 20,
    }
    """
    cfg_path = Path(config_path) if config_path else CONFIG_PATH

    if not cfg_path.exists():
        logger.warning("配置文件未找到，使用默认时序参数", path=str(cfg_path))
        return dict(_DEFAULT_TS_PARAMS)

    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    ts_cfg = cfg.get("timeseries", {})

    if not ts_cfg:
        logger.warning("配置文件中无 timeseries 段，使用默认参数")
        return dict(_DEFAULT_TS_PARAMS)

    # 合并默认值，只使用配置中存在的键
    result = dict(_DEFAULT_TS_PARAMS)
    for key in ts_cfg:
        if key in result:
            result[key] = ts_cfg[key]
        else:
            logger.warning("配置文件中未知的 timeseries 键: %s", key, key=key)

    return result


def compute_delay(series: pd.Series, period: int) -> pd.Series:
    """
    计算 DELAY（滞后值）。
    将序列向后移动 period 个位置，前 period 个值为 NaN。

    参数:
        series: 输入序列（通常为 close）
        period: 滞后周期（标准 1）

    返回:
        名为 DELAY_{period} 的 Series
    """
    delayed = series.shift(period)
    delayed.name = f"DELAY_{period}"
    return delayed


def compute_delta(series: pd.Series, period: int) -> pd.Series:
    """
    计算 DELTA（差值 = 当前值 - period 前的值）。

    参数:
        series: 输入序列（通常为 close）
        period: 差分周期（标准 1）

    返回:
        名为 DELTA_{period} 的 Series
    """
    diff = series.diff(period)
    diff.name = f"DELTA_{period}"
    return diff


def compute_ts_max(series: pd.Series, period: int) -> pd.Series:
    """
    计算 TS_MAX（滚动窗口最大值）。

    参数:
        series: 输入序列
        period: 滚动窗口大小（标准 20）

    返回:
        名为 TS_MAX_{period} 的 Series
    """
    rolling_max = series.rolling(window=period, min_periods=1).max()
    rolling_max.name = f"TS_MAX_{period}"
    return rolling_max


def compute_ts_min(series: pd.Series, period: int) -> pd.Series:
    """
    计算 TS_MIN（滚动窗口最小值）。

    参数:
        series: 输入序列
        period: 滚动窗口大小（标准 20）

    返回:
        名为 TS_MIN_{period} 的 Series
    """
    rolling_min = series.rolling(window=period, min_periods=1).min()
    rolling_min.name = f"TS_MIN_{period}"
    return rolling_min


def compute_ts_rank(series: pd.Series, period: int) -> pd.Series:
    """
    计算 TS_RANK（滚动窗口内的百分位排名，0~1）。
    将当前值在过去 period 个值中的相对位置映射到 [0, 1]。

    参数:
        series: 输入序列
        period: 滚动窗口大小（标准 20）

    返回:
        名为 TS_RANK_{period} 的 Series
    """
    def _rank_last(window: pd.Series) -> float:
        """窗口内最后一个值的百分位排名"""
        last_val = window.iloc[-1]
        rank = window.rank(pct=True).iloc[-1]
        return rank

    ranked = series.rolling(window=period, min_periods=period).apply(_rank_last, raw=False)
    ranked.name = f"TS_RANK_{period}"
    return ranked


def compute_ts_zscore(series: pd.Series, period: int) -> pd.Series:
    """
    计算 TS_ZSCORE（滚动窗口 Z-Score = (当前值 - 均值) / 标准差）。
    衡量当前值相对于历史均值的偏离程度。

    参数:
        series: 输入序列
        period: 滚动窗口大小（标准 20）

    返回:
        名为 TS_ZSCORE_{period} 的 Series
    """
    rolling_mean = series.rolling(window=period, min_periods=period).mean()
    rolling_std = series.rolling(window=period, min_periods=period).std(ddof=0)

    zscore = (series - rolling_mean) / rolling_std.replace(0, np.nan)
    zscore.name = f"TS_ZSCORE_{period}"
    return zscore


def compute_corr(series_a: pd.Series, series_b: pd.Series, period: int) -> pd.Series:
    """
    计算 CORR（滚动相关系数）。

    参数:
        series_a: 输入序列 A（如 close）
        series_b: 输入序列 B（如 volume）
        period: 滚动窗口大小（标准 20）

    返回:
        名为 CORR_{period} 的 Series
    """
    corr = series_a.rolling(window=period, min_periods=period).corr(series_b)
    corr.name = f"CORR_{period}"
    return corr


def compute_timeseries(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """
    计算所有时序操作指标（DELAY + DELTA + TS_MAX + TS_MIN + TS_RANK + TS_ZSCORE + CORR），
    追加到 DataFrame。

    参数:
        df: OHLCV DataFrame（列: open, high, low, close, volume）
        cfg: indicators.yml 的 timeseries 段配置。为 None 时自动读取。

    返回:
        追加了以下列的 DataFrame（默认在 close 列上计算）:
        - DELAY_{period}
        - DELTA_{period}
        - TS_MAX_{period}
        - TS_MIN_{period}
        - TS_RANK_{period}
        - TS_ZSCORE_{period}
        - CORR_{period}（close 与 volume 的相关系数）

    注意:
        - 不丢弃任何行，数据不足时对应位置为 NaN
        - 纯函数，不修改输入 df
        - TS_RANK/TS_ZSCORE/CORR 需要至少 period 个数据点
    """
    df = df.copy()

    if cfg is None:
        cfg = load_timeseries_params()

    for col in ["close", "volume"]:
        if col not in df.columns:
            logger.warning("DataFrame 缺少必要列 '%s'", col, column=col)
            return df

    if not pd.api.types.is_numeric_dtype(df["close"]):
        logger.warning("'close' 列必须为数值类型")
        return df

    try:
        close = df["close"]
        volume = df["volume"]

        df[f"DELAY_{cfg['delay_period']}"] = compute_delay(close, cfg["delay_period"])
        df[f"DELTA_{cfg['delta_period']}"] = compute_delta(close, cfg["delta_period"])
        df[f"TS_MAX_{cfg['ts_max_period']}"] = compute_ts_max(close, cfg["ts_max_period"])
        df[f"TS_MIN_{cfg['ts_min_period']}"] = compute_ts_min(close, cfg["ts_min_period"])
        df[f"TS_RANK_{cfg['ts_rank_period']}"] = compute_ts_rank(close, cfg["ts_rank_period"])
        df[f"TS_ZSCORE_{cfg['ts_zscore_period']}"] = compute_ts_zscore(close, cfg["ts_zscore_period"])
        df[f"CORR_{cfg['corr_period']}"] = compute_corr(close, volume, cfg["corr_period"])

    except Exception as e:
        logger.error("时序操作指标计算异常", error=str(e))

    return df
