"""
模块名称: volume.py
所属层级: 指标计算层 (Indicators)
输入来源: OHLCV DataFrame（列: open, high, low, close, volume）
输出去向: 追加成交量指标列的 DataFrame（NaN 保留，不丢弃行）
关键依赖: pandas, numpy, structlog, yaml
（无外部第三方指标库依赖，全部使用 pandas/numpy 原生实现）

修订记录:
- v2.0: 移除 pandas_ta 依赖，全部改用 pandas/numpy 原生实现
        OBV → cumsum(volume × sign(close diff))
        VWAP → cumsum(volume × typical_price) / cumsum(volume)
        MFI → RSI-like with money flow
        CMF → Chaikin Money Flow
        VOL_RATIO → volume / SMA(volume)
- v1.0: 初始实现，OBV + VWAP + MFI(14) + CMF(20) + VOL_RATIO(20)
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
def load_volume_params(config_path: str | Path | None = None) -> dict[str, Any]:
    """
    从 config/indicators.yml 的 volume 段读取参数。

    返回结构:
    {
        "mfi_period": 14,
        "cmf_period": 20,
        "vol_ratio_period": 20
    }
    """
    cfg_path = Path(config_path) if config_path else CONFIG_PATH

    if not cfg_path.exists():
        logger.warning("配置文件未找到，使用默认参数", path=str(cfg_path))
        return {
            "mfi_period": 14,
            "cmf_period": 20,
            "vol_ratio_period": 20,
        }

    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    vol_cfg = cfg.get("volume", {})
    defaults = {
        "mfi_period": 14,
        "cmf_period": 20,
        "vol_ratio_period": 20,
    }

    for key in defaults:
        if key not in vol_cfg:
            logger.warning("配置文件缺少 volume.%s，使用默认值", key, key=key, default=defaults[key])

    return {**defaults, **vol_cfg}


def compute_obv(df: pd.DataFrame) -> pd.Series:
    """
    计算 OBV（能量潮指标）。
    不需要周期参数，基于 close 与 volume 的累积计算。

    返回:
        名为 OBV 的 Series
    """
    for col in ["close", "volume"]:
        if col not in df.columns:
            logger.warning("DataFrame 缺少 '%s' 列", col, column=col)
            return pd.Series(index=df.index, dtype=float, name="OBV")

        """
        纯 pandas 实现 OBV（能量潮指标）。

        算法:
            OBV = cumsum(volume × sign(close - prev_close))
        """
        close = df["close"]
        volume = df["volume"]

        # 价格方向: 1=上涨, -1=下跌, 0=持平
        price_direction = np.sign(close.diff()).fillna(0)

        obv = (price_direction * volume).cumsum()
        obv.name = "OBV"
        return obv


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """
    计算 VWAP（成交量加权平均价格）。
    使用 pandas_ta 的 vwap 实现，默认基于全部数据计算。

    注意:
        VWAP 在日内交易中通常每日重置，但此处按全量数据计算。
        若需日级别重置，上层需按日分组后分别调用。

    返回:
        名为 VWAP 的 Series
    """
    for col in ["high", "low", "close", "volume"]:
        if col not in df.columns:
            logger.warning("DataFrame 缺少 '%s' 列", col, column=col)
            return pd.Series(index=df.index, dtype=float, name="VWAP")

        """
    纯 pandas 实现 VWAP（成交量加权平均价格）。

    算法:
        VWAP = cumsum(volume × typical_price) / cumsum(volume)
    """
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = df["volume"] * typical_price

    vwap = pv.cumsum() / df["volume"].cumsum().replace(0, np.nan)
    vwap.name = "VWAP"
    return vwap


def compute_mfi(df: pd.DataFrame, period: int) -> pd.Series:
    """
    计算 MFI（资金流量指标）。

    参数:
        df: 含 high, low, close, volume 列的 DataFrame
        period: MFI 回溯周期（标准 14）

    返回:
        名为 MFI_{period} 的 Series
    """
    for col in ["high", "low", "close", "volume"]:
        if col not in df.columns:
            logger.warning("DataFrame 缺少 '%s' 列", col, column=col)
            return pd.Series(index=df.index, dtype=float, name=f"MFI_{period}")

            """
    纯 pandas 实现 MFI（资金流量指标）。

    算法（类似 RSI，但使用成交量加权）:
        1. typical_price = (H + L + C) / 3
        2. raw_money_flow = typical_price × volume
        3. 价格上升时 → positive money flow
        4. 价格下降时 → negative money flow
        5. money_ratio = sum(pos) / sum(neg)
        6. MFI = 100 - (100 / (1 + money_ratio))
    """
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    money_flow = typical_price * df["volume"]

    # 价格方向
    price_diff = typical_price.diff()
    positive_flow = money_flow.where(price_diff > 0, 0.0)
    negative_flow = money_flow.where(price_diff < 0, 0.0)

    # 滚动求和
    pos_sum = positive_flow.rolling(window=period, min_periods=period).sum()
    neg_sum = negative_flow.rolling(window=period, min_periods=period).sum()

    money_ratio = pos_sum / neg_sum.replace(0, np.nan)
    mfi = 100.0 - (100.0 / (1.0 + money_ratio))

    mfi.name = f"MFI_{period}"
    return mfi


def compute_cmf(df: pd.DataFrame, period: int) -> pd.Series:
    """
    计算 CMF（Chaikin 资金流量）。

    参数:
        df: 含 high, low, close, volume 列的 DataFrame
        period: CMF 回溯周期（标准 20）

    返回:
        名为 CMF_{period} 的 Series
    """
    for col in ["high", "low", "close", "volume"]:
        if col not in df.columns:
            logger.warning("DataFrame 缺少 '%s' 列", col, column=col)
            return pd.Series(index=df.index, dtype=float, name=f"CMF_{period}")

            """
    纯 pandas 实现 CMF（Chaikin 资金流量）。

    算法:
        1. Money Flow Multiplier = ((C - L) - (H - C)) / (H - L)
        2. Money Flow Volume = Multiplier × Volume
        3. CMF = sum(MFV, period) / sum(Volume, period)
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]
    volume = df["volume"]

    hl_range = high - low
    # Money Flow Multiplier: ((C - L) - (H - C)) / (H - L)
    mf_multiplier = ((close - low) - (high - close)) / hl_range.replace(0, np.nan)
    mf_volume = mf_multiplier * volume

    cmf = mf_volume.rolling(window=period, min_periods=period).sum() / \
          volume.rolling(window=period, min_periods=period).sum().replace(0, np.nan)

    cmf.name = f"CMF_{period}"
    return cmf


def compute_vol_ratio(df: pd.DataFrame, period: int) -> pd.Series:
    """
    计算 VOL_RATIO（成交量比率 = 当前成交量 / 过去 period 平均成交量）。

    参数:
        df: 含 volume 列的 DataFrame
        period: 成交量均线周期（标准 20）

    返回:
        名为 VOL_RATIO_{period} 的 Series
    """
    if "volume" not in df.columns:
        logger.warning("DataFrame 缺少 'volume' 列")
        return pd.Series(index=df.index, dtype=float, name=f"VOL_RATIO_{period}")

    vol_sma = df["volume"].rolling(window=period, min_periods=period).mean()
    vol_ratio = df["volume"] / vol_sma.replace(0, np.nan)
    vol_ratio.name = f"VOL_RATIO_{period}"
    return vol_ratio


def compute_volume(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """
    计算所有成交量指标（OBV + VWAP + MFI + CMF + VOL_RATIO），追加到 DataFrame。

    参数:
        df: OHLCV DataFrame（列: open, high, low, close, volume）
        cfg: indicators.yml 的 volume 段配置。为 None 时自动读取。

    返回:
        追加了以下列的 DataFrame:
        - OBV
        - VWAP
        - MFI_{period}
        - CMF_{period}
        - VOL_RATIO_{period}

    注意:
        - 不丢弃任何行，数据不足时对应位置为 NaN
        - 纯函数，不修改输入 df
    """
    df = df.copy()

    if cfg is None:
        cfg = load_volume_params()

    # 验证必要列
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            logger.warning("DataFrame 缺少必要列 '%s'", col, column=col)
            return df

    if not pd.api.types.is_numeric_dtype(df["close"]):
        logger.warning("'close' 列必须为数值类型")
        return df

    try:
        # OBV（无周期参数）
        df["OBV"] = compute_obv(df)

        # VWAP（无周期参数）
        df["VWAP"] = compute_vwap(df)

        # MFI
        df[f"MFI_{cfg['mfi_period']}"] = compute_mfi(df, cfg["mfi_period"])

        # CMF
        df[f"CMF_{cfg['cmf_period']}"] = compute_cmf(df, cfg["cmf_period"])

        # VOL_RATIO
        df[f"VOL_RATIO_{cfg['vol_ratio_period']}"] = compute_vol_ratio(df, cfg["vol_ratio_period"])

    except Exception as e:
        logger.error("成交量指标计算异常", error=str(e))

    return df
