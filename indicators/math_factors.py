"""
模块名称: math_factors.py
所属层级: 指标计算层 (Indicators)
输入来源: OHLCV DataFrame（列: open, high, low, close, volume）
输出去向: 追加数学变换因子列的 DataFrame（NaN 保留，不丢弃行）
关键依赖: pandas, numpy, structlog, yaml

功能说明:
    计算纯数学变换因子，作为机器学习特征工程的基础工具。
    所有计算仅依赖 pandas/numpy，无需 pandas_ta。

    包含因子:
    - LOG_RETURN_{period}: 对数收益率 log(close / close.shift(period))
    - ZSCORE_{period}:     滚动 Z-Score 标准化 (x - mean) / std
    - RANK_{period}:       滚动百分位排名 0~100
    - SIGN:                价格方向信号 1 / -1 / 0
    - ABS_RETURN_{period}: 绝对对数收益率 |log(close / close.shift(period))|
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import structlog
import yaml

logger = structlog.get_logger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent / "config"
CONFIG_PATH = _CONFIG_DIR / "indicators.yml"


# ─── 配置加载 ──────────────────────────────────────────


@lru_cache(maxsize=1)
def load_math_factors_params(config_path: str | Path | None = None) -> dict[str, Any]:
    """
    从 config/indicators.yml 的 math_factors 段读取参数。
    结果被 lru_cache 缓存，只在首次调用时读文件。

    返回结构:
    {
        "log_return_period": 1,
        "zscore_period": 20,
        "rank_period": 20,
        "abs_return_period": 1,
    }
    """
    cfg_path = Path(config_path) if config_path else CONFIG_PATH

    if not cfg_path.exists():
        logger.warning("配置文件未找到，使用默认参数", path=str(cfg_path))
        return {
            "log_return_period": 1,
            "zscore_period": 20,
            "rank_period": 20,
            "abs_return_period": 1,
        }

    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    math_cfg = cfg.get("math_factors", {})

    defaults = {
        "log_return_period": 1,
        "zscore_period": 20,
        "rank_period": 20,
        "abs_return_period": 1,
    }
    for key in defaults:
        if key not in math_cfg:
            logger.warning("配置文件缺少 math_factors.%s，使用默认值", key, key=key, default=defaults[key])

    return {**defaults, **math_cfg}


# ─── 核心计算函数 ─────────────────────────────────────────


def compute_log_return(df: pd.DataFrame, period: int) -> pd.Series:
    """
    计算对数收益率: log(close / close.shift(period))

    参数:
        df: 含 close 列的 DataFrame
        period: 滞后阶数（1 为单期收益，>1 为多期累积收益）

    返回:
        名为 LOG_RETURN_{period} 的 Series
    """
    if "close" not in df.columns:
        logger.warning("DataFrame 缺少 'close' 列")
        return pd.Series(index=df.index, dtype=float, name=f"LOG_RETURN_{period}")

    if not pd.api.types.is_numeric_dtype(df["close"]):
        logger.warning("'close' 列必须为数值类型")
        return pd.Series(index=df.index, dtype=float, name=f"LOG_RETURN_{period}")

    # 避免 log(0) 或负数
    close = df["close"].replace(0, np.nan).where(df["close"] > 0, np.nan)
    log_ret = np.log(close / close.shift(period))
    log_ret.name = f"LOG_RETURN_{period}"
    return log_ret


def compute_zscore(df: pd.DataFrame, period: int) -> pd.Series:
    """
    计算滚动 Z-Score: (close - rolling_mean) / rolling_std

    参数:
        df: 含 close 列的 DataFrame
        period: 滚动窗口大小

    返回:
        名为 ZSCORE_{period} 的 Series
    """
    if "close" not in df.columns:
        logger.warning("DataFrame 缺少 'close' 列")
        return pd.Series(index=df.index, dtype=float, name=f"ZSCORE_{period}")

    if not pd.api.types.is_numeric_dtype(df["close"]):
        logger.warning("'close' 列必须为数值类型")
        return pd.Series(index=df.index, dtype=float, name=f"ZSCORE_{period}")

    rolling_mean = df["close"].rolling(window=period).mean()
    rolling_std = df["close"].rolling(window=period).std(ddof=0)

    # 避免除零
    zscore = (df["close"] - rolling_mean) / rolling_std.replace(0, np.nan)
    zscore.name = f"ZSCORE_{period}"
    return zscore


def compute_rank(df: pd.DataFrame, period: int) -> pd.Series:
    """
    计算滚动百分位排名 (0~100)。
    表示当前 close 在滚动窗口中的相对位置。

    参数:
        df: 含 close 列的 DataFrame
        period: 滚动窗口大小

    返回:
        名为 RANK_{period} 的 Series，值域 [0, 100]
    """
    if "close" not in df.columns:
        logger.warning("DataFrame 缺少 'close' 列")
        return pd.Series(index=df.index, dtype=float, name=f"RANK_{period}")

    if not pd.api.types.is_numeric_dtype(df["close"]):
        logger.warning("'close' 列必须为数值类型")
        return pd.Series(index=df.index, dtype=float, name=f"RANK_{period}")

    def _percentile_rank(series: pd.Series) -> float:
        """将窗口内最后一个值的百分位排名映射到 [0, 100]。"""
        last_val = series.iloc[-1]
        count_less = (series < last_val).sum()
        count_equal = (series == last_val).sum()
        # 使用平均排名计算方法
        rank = (count_less + 0.5 * count_equal) / len(series) * 100.0
        return rank

    rank = df["close"].rolling(window=period).apply(_percentile_rank, raw=False)
    rank.name = f"RANK_{period}"
    return rank


def compute_sign(df: pd.DataFrame) -> pd.Series:
    """
    计算价格方向信号:
    1  = 收盘价上涨 (close > open)
    -1 = 收盘价下跌 (close < open)
    0  = 收盘价持平 (close == open)

    返回:
        名为 SIGN 的 Series
    """
    required = ["open", "close"]
    for col in required:
        if col not in df.columns:
            logger.warning("DataFrame 缺少 '%s' 列", col, column=col)
            return pd.Series(index=df.index, dtype=float, name="SIGN")

    sign = np.sign(df["close"] - df["open"])
    sign = sign.astype(float)
    sign.name = "SIGN"
    return sign


def compute_abs_return(df: pd.DataFrame, period: int) -> pd.Series:
    """
    计算绝对对数收益率: |log(close / close.shift(period))|

    适用于衡量波动幅度（不考虑方向）。

    参数:
        df: 含 close 列的 DataFrame
        period: 滞后阶数

    返回:
        名为 ABS_RETURN_{period} 的 Series
    """
    if "close" not in df.columns:
        logger.warning("DataFrame 缺少 'close' 列")
        return pd.Series(index=df.index, dtype=float, name=f"ABS_RETURN_{period}")

    if not pd.api.types.is_numeric_dtype(df["close"]):
        logger.warning("'close' 列必须为数值类型")
        return pd.Series(index=df.index, dtype=float, name=f"ABS_RETURN_{period}")

    close = df["close"].replace(0, np.nan).where(df["close"] > 0, np.nan)
    abs_ret = np.abs(np.log(close / close.shift(period)))
    abs_ret.name = f"ABS_RETURN_{period}"
    return abs_ret


# ─── 主入口 ──────────────────────────────────────────


def compute_math_factors(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """
    计算所有数学变换因子，追加到 DataFrame。

    参数:
        df: OHLCV DataFrame（列: open, high, low, close, volume）
        cfg: indicators.yml 的 math_factors 段配置。为 None 时自动读取。

    返回:
        追加了以下列的 DataFrame:
        - LOG_RETURN_{period}
        - ZSCORE_{period}
        - RANK_{period}
        - SIGN
        - ABS_RETURN_{period}

    注意:
        - 不丢弃任何行，数据不足时对应位置为 NaN
        - 纯函数，不修改输入 df
        - 所有计算仅依赖 pandas/numpy，无需 pandas_ta
    """
    df = df.copy()

    if cfg is None:
        cfg = load_math_factors_params()

    # 验证必要列存在
    if "close" not in df.columns:
        logger.warning("DataFrame 缺少必要列 'close'")
        return df

    if not pd.api.types.is_numeric_dtype(df["close"]):
        logger.warning("'close' 列必须为数值类型")
        return df

    try:
        # LOG_RETURN
        lr_period = int(cfg["log_return_period"])
        df[f"LOG_RETURN_{lr_period}"] = compute_log_return(df, lr_period)
        logger.debug("LOG_RETURN 计算完成", period=lr_period)

        # ZSCORE
        zs_period = int(cfg["zscore_period"])
        df[f"ZSCORE_{zs_period}"] = compute_zscore(df, zs_period)
        logger.debug("ZSCORE 计算完成", period=zs_period)

        # RANK
        rk_period = int(cfg["rank_period"])
        df[f"RANK_{rk_period}"] = compute_rank(df, rk_period)
        logger.debug("RANK 计算完成", period=rk_period)

        # SIGN
        df["SIGN"] = compute_sign(df)
        logger.debug("SIGN 计算完成")

        # ABS_RETURN
        ar_period = int(cfg["abs_return_period"])
        df[f"ABS_RETURN_{ar_period}"] = compute_abs_return(df, ar_period)
        logger.debug("ABS_RETURN 计算完成", period=ar_period)

    except Exception as e:
        logger.error("数学变换因子计算异常", error=str(e))

    return df

