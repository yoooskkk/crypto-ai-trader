"""
模块名称: momentum.py
所属层级: 指标计算层 (Indicators)
输入来源: OHLCV DataFrame（列: open, high, low, close, volume）
输出去向: 追加动量指标列的 DataFrame（NaN 保留，不丢弃行）
关键依赖: pandas, numpy, structlog, yaml
（无外部第三方指标库依赖，全部使用 pandas/numpy 原生实现）

修订记录:
- v3.0: 移除 pandas_ta 依赖，全部改用 pandas/numpy 原生实现
        RSI → pure pandas (EMA of gains/losses)
        ROC → pct_change
        CCI → typical price - SMA / (0.015 × Mean Absolute Deviation)
        STOCH → %K = (close - LL) / (HH - LL)
- v2.0: 重写为 DataFrame→DataFrame 模式，统一与 trend.py 接口风格
        补 CCI(20)，对齐 config/indicators.yml 键名，统一使用 structlog
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
def load_momentum_params(config_path: str | Path | None = None) -> dict[str, Any]:
    """
    从 config/indicators.yml 的 momentum 段读取参数。
    结果被 lru_cache 缓存，只在首次调用时读文件。

    返回结构:
    {
        "rsi_period": 14,
        "roc_period": 10,
        "cci_period": 20,
        "stoch": {"k": 14, "d": 3, "smooth_k": 3}
    }
    """
    cfg_path = Path(config_path) if config_path else CONFIG_PATH

    if not cfg_path.exists():
        logger.warning("配置文件未找到，使用默认参数", path=str(cfg_path))
        return {
            "rsi_period": 14,
            "roc_period": 10,
            "cci_period": 20,
            "stoch": {"k": 14, "d": 3, "smooth_k": 3},
        }

    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    momentum_cfg = cfg.get("momentum", {})

    # 合并默认值，确保缺失键也有值
    defaults = {
        "rsi_period": 14,
        "roc_period": 10,
        "cci_period": 20,
        "stoch": {"k": 14, "d": 3, "smooth_k": 3},
    }
    for key in defaults:
        if key not in momentum_cfg:
            logger.warning("配置文件缺少 momentum.%s，使用默认值", key, key=key, default=defaults[key])

    return {**defaults, **momentum_cfg}


def compute_rsi(df: pd.DataFrame, period: int) -> pd.Series:
    """
    计算 RSI（相对强弱指标）。

    参数:
        df: 含 close 列的 DataFrame
        period: RSI 回溯周期（标准 14）

    返回:
        名为 RSI_{period} 的 Series，数据不足时前 period 个值为 NaN
    """
    if "close" not in df.columns:
        logger.warning("DataFrame 缺少 'close' 列")
        return pd.Series(index=df.index, dtype=float, name=f"RSI_{period}")

        """
    纯 pandas 实现 RSI（相对强弱指标）。

    算法:
        1. 计算每日价格变化 delta
        2. 分离上涨日(gain)和下跌日(loss)
        3. 用 EMA 平滑 gain 和 loss 的平均值
        4. RS = avg_gain / avg_loss
        5. RSI = 100 - (100 / (1 + RS))
    """
    close = df["close"]
    delta = close.diff()

    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0))

    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))

    rsi.name = f"RSI_{period}"
    return rsi


def compute_roc(df: pd.DataFrame, period: int) -> pd.Series:
    """
    计算 ROC（变动率指标）。

    参数:
        df: 含 close 列的 DataFrame
        period: ROC 回溯周期（标准 10）

    返回:
        名为 ROC_{period} 的 Series
    """
    if "close" not in df.columns:
        logger.warning("DataFrame 缺少 'close' 列")
        return pd.Series(index=df.index, dtype=float, name=f"ROC_{period}")

            roc = df["close"].pct_change(periods=period) * 100.0
    roc.name = f"ROC_{period}"
    return roc


def compute_cci(df: pd.DataFrame, period: int) -> pd.Series:
    """
    计算 CCI（商品通道指数）。

    参数:
        df: 含 high, low, close 列的 DataFrame
        period: CCI 回溯周期（标准 20）

    返回:
        名为 CCI_{period} 的 Series
    """
    required = ["high", "low", "close"]
    for col in required:
        if col not in df.columns:
            logger.warning("DataFrame 缺少 '%s' 列", col, column=col)
            return pd.Series(index=df.index, dtype=float, name=f"CCI_{period}")

        """
    纯 pandas 实现 CCI（商品通道指数）。

    算法:
        1. TP = (high + low + close) / 3
        2. SMA_TP = SMA(TP, period)
        3. MAD = Mean Absolute Deviation of TP
        4. CCI = (TP - SMA_TP) / (0.015 × MAD)
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    tp_ma = tp.rolling(window=period, min_periods=period).mean()

    # Mean Absolute Deviation
    mad = tp.rolling(window=period, min_periods=period).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True
    )

    cci = (tp - tp_ma) / (0.015 * mad.replace(0, np.nan))
    cci.name = f"CCI_{period}"
    return cci


def compute_stoch(df: pd.DataFrame, k: int, d: int, smooth_k: int) -> tuple[pd.Series, pd.Series]:
    """
    计算 Stochastic Oscillator（随机指标 %K 和 %D）。

    参数:
        df: 含 high, low, close 列的 DataFrame
        k: %K 周期（标准 14）
        d: %D 平滑周期（标准 3）
        smooth_k: %K 内部平滑（标准 3）

    返回:
        (STOCH_K_{k}_{d}, STOCH_D_{k}_{d}) 的 tuple
    """
    required = ["high", "low", "close"]
    for col in required:
        if col not in df.columns:
            logger.warning("DataFrame 缺少 '%s' 列", col, column=col)
            empty = pd.Series(index=df.index, dtype=float, name=f"STOCH_K_{k}_{d}")
            return empty, empty.copy()

        """
    纯 pandas 实现 Stochastic Oscillator（随机指标 %K 和 %D）。

    算法:
        %K = SMA((close - LL) / (HH - LL) × 100, smooth_k)
        %D = SMA(%K, d)
        其中 LL = 最低价的最低值, HH = 最高价的最高值
    """
    high_k = df["high"].rolling(window=k, min_periods=k).max()
    low_k = df["low"].rolling(window=k, min_periods=k).min()

    # %K raw: (close - LL) / (HH - LL) * 100
    range_k = high_k - low_k
    stoch_raw = 100.0 * (df["close"] - low_k) / range_k.replace(0, np.nan)

    # 平滑 %K
    stoch_k = stoch_raw.rolling(window=smooth_k, min_periods=1).mean()

    # %D = SMA of %K
    stoch_d = stoch_k.rolling(window=d, min_periods=1).mean()

    stoch_k = stoch_k.rename(f"STOCH_K_{k}_{d}")
    stoch_d = stoch_d.rename(f"STOCH_D_{k}_{d}")
    return stoch_k, stoch_d


def compute_momentum(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """
    计算所有动量指标（RSI + ROC + CCI + STOCH），追加到 DataFrame。

    参数:
        df: OHLCV DataFrame（列: open, high, low, close, volume）
        cfg: indicators.yml 的 momentum 段配置。为 None 时自动读取。

    返回:
        追加了以下列的 DataFrame:
        - RSI_{period}
        - ROC_{period}
        - CCI_{period}
        - STOCH_K_{k}_{d}
        - STOCH_D_{k}_{d}

    注意:
        - 不丢弃任何行，数据不足时对应位置为 NaN
        - 纯函数，不修改输入 df
    """
    df = df.copy()

    if cfg is None:
        cfg = load_momentum_params()

    # 验证必要列存在
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            logger.warning("DataFrame 缺少必要列 '%s'", col, column=col)
            return df

    if not pd.api.types.is_numeric_dtype(df["close"]):
        logger.warning("'close' 列必须为数值类型")
        return df

    try:
        # RSI
        df[f"RSI_{cfg['rsi_period']}"] = compute_rsi(df, cfg["rsi_period"])

        # ROC
        df[f"ROC_{cfg['roc_period']}"] = compute_roc(df, cfg["roc_period"])

        # CCI
        df[f"CCI_{cfg['cci_period']}"] = compute_cci(df, cfg["cci_period"])

        # STOCH
        stoch_k, stoch_d = compute_stoch(
            df,
            k=cfg["stoch"]["k"],
            d=cfg["stoch"]["d"],
            smooth_k=cfg["stoch"].get("smooth_k", 3),
        )
        df[stoch_k.name] = stoch_k
        df[stoch_d.name] = stoch_d

    except Exception as e:
        logger.error("动量指标计算异常", error=str(e))

    return df