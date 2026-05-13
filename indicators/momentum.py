"""
Module: indicators/momentum.py
Layer:  Indicator

=== INPUT CONTRACT ===
Source:  Called by indicator-worker after consuming Redis Stream: raw_kline
Schema:  pd.DataFrame with columns [open_time, open, high, low, close, volume]
         Index: DatetimeIndex (UTC); dtypes: float64 for OHLCV
Trigger: Every finalized bar (is_closed=true) on any subscribed timeframe

=== OUTPUT CONTRACT ===
Dest:    Returns Dict[str, float | None]
Schema:  Keys: RSI_{period}, STOCHRSIk_{k}_{d}, STOCHRSId_{k}_{d},
               ROC_{period}
         Values: float (computed) or None (insufficient history)
         Merged into indicators stream payload by indicator-worker
SLA:     < 10ms per bar (vectorized pandas_ta computation)

=== KEY DEPENDENCIES ===
Internal: None (no caching needed; all TFs compute on the fly)
External: pandas-ta >= 0.3.14b, pandas, structlog
Config:   config/indicators.yml (all period parameters; never hardcode)

=== INVARIANTS ===
- All period parameters MUST be read from config/indicators.yml; no numeric literals in code.
- Return None (not 0.0) when history is insufficient — consumers must handle None explicitly.
- Do not write to any Redis Stream directly; return dict to caller only.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
import structlog
import yaml

logger = structlog.get_logger(__name__)


@lru_cache(maxsize=1)  # M1: cache config reads
def load_momentum_params(config_path: str | Path = "config/indicators.yml") -> dict[str, Any]:
    """
    Load momentum indicator parameters from the YAML config file.

    Args:
        config_path: Path to indicators.yml, relative or absolute.

    Returns:
        Dictionary with keys: rsi_period, stochrsi_k, stochrsi_d, stochrsi_period, roc_period.
        Defaults are provided if keys are missing.

    The expected YAML structure:
        momentum:
          rsi_period: 14
          stochrsi_k: 3
          stochrsi_d: 3
          stochrsi_period: 14
          roc_period: 10
    """
    config_file = Path(config_path)
    if not config_file.exists():
        logger.warning("Config file not found, using defaults", path=str(config_file))
        return {
            "rsi_period": 14,
            "stochrsi_k": 3,
            "stochrsi_d": 3,
            "stochrsi_period": 14,
            "roc_period": 10,
        }

    with open(config_file, "r") as f:
        config = yaml.safe_load(f)

    momentum_cfg = config.get("momentum", {})
    defaults = {
        "rsi_period": 14,
        "stochrsi_k": 3,
        "stochrsi_d": 3,
        "stochrsi_period": 14,
        "roc_period": 10,
    }
    for key in defaults:
        if key not in momentum_cfg:
            logger.warning("Missing momentum config key, using default", key=key, default=defaults[key])

    return {**defaults, **momentum_cfg}


def compute_rsi(
    df: pd.DataFrame,
    period: int,
) -> pd.Series:
    """
    Compute Relative Strength Index (RSI) using pandas_ta.

    Args:
        df: DataFrame with a 'close' column.
        period: RSI lookback period (commonly 14).

    Returns:
        Series with name 'RSI_{period}', NaN for insufficient data.
    """
    if "close" not in df:
        logger.warning("DataFrame missing 'close' column")  # M4: debug → warning
        return pd.Series(index=df.index, dtype=float)

    # M4: removed logger.debug
    rsi = df.ta.rsi(length=period, append=False)
    rsi.name = f"RSI_{period}"
    return rsi


def compute_stochrsi(
    df: pd.DataFrame,
    k: int,
    d: int,
    period: int,
) -> tuple[pd.Series, pd.Series]:
    """
    Compute Stochastic RSI (%K and %D lines) using pandas_ta.

    Args:
        df: DataFrame with 'high', 'low', 'close' columns.
        k: %K smoothing period (typically 3).
        d: %D smoothing period (typically 3).
        period: RSI lookback for Stochastic RSI.

    Returns:
        Tuple of (stochrsi_k_series, stochrsi_d_series).
        Each series has NaN at the beginning when insufficient data.
    """
    required_cols = ["high", "low", "close"]
    for col in required_cols:
        if col not in df:
            logger.warning("DataFrame missing required column", column=col)  # M4
            return pd.Series(index=df.index, dtype=float), pd.Series(index=df.index, dtype=float)

    # M4: removed logger.debug
    stochrsicalc = df.ta.stochrsi(length=period, k=k, d=d, append=False)

    # M2: Rename to match OUTPUT CONTRACT: STOCHRSIk_{k}_{d}, STOCHRSId_{k}_{d}
    # (remove the period prefix that pandas_ta adds)
    k_col_target = f"STOCHRSIk_{k}_{d}"
    d_col_target = f"STOCHRSId_{k}_{d}"

    # Build Series with clean names
    stochk = stochrsicalc.iloc[:, 0].rename(k_col_target)
    stochd = stochrsicalc.iloc[:, 1].rename(d_col_target)
    return stochk, stochd


def compute_roc(
    df: pd.DataFrame,
    period: int,
) -> pd.Series:
    """
    Compute Rate of Change (ROC) using pandas_ta.

    Args:
        df: DataFrame with 'close' column.
        period: Lookback period (common 10).

    Returns:
        Series with name 'ROC_{period}', NaN for insufficient history.
    """
    if "close" not in df:
        logger.warning("DataFrame missing 'close' column")  # M4
        return pd.Series(index=df.index, dtype=float)

    # M4: removed logger.debug
    roc = df.ta.roc(length=period, append=False)
    roc.name = f"ROC_{period}"
    return roc


def compute_momentum(
    df: pd.DataFrame,
    params: dict[str, Any] | None = None,
) -> dict[str, float | None]:
    """
    Compute all momentum indicators and return the latest values as a dict.

    Args:
        df: OHLCV DataFrame with at least one row. Must have columns: open, high, low, close, volume.
        params: (Optional) Parameter dictionary from load_momentum_params().
                If None, load defaults.

    Returns:
        Dictionary with keys:
          - RSI_{period}
          - STOCHRSIk_{k}_{d}
          - STOCHRSId_{k}_{d}
          - ROC_{period}
        Values are float if available, else None.
    """
    if params is None:
        params = load_momentum_params()

    rsi_period = params["rsi_period"]
    stochrsi_k = params["stochrsi_k"]
    stochrsi_d = params["stochrsi_d"]
    stochrsi_period = params["stochrsi_period"]
    roc_period = params["roc_period"]

    # M3: guard for insufficient data
    min_required = max(rsi_period, stochrsi_period, roc_period) * 2
    if df.empty or len(df) < min_required:
        logger.warning("DataFrame too short for momentum calculation", length=len(df))
        return {
            f"RSI_{rsi_period}": None,
            f"STOCHRSIk_{stochrsi_k}_{stochrsi_d}": None,
            f"STOCHRSId_{stochrsi_k}_{stochrsi_d}": None,
            f"ROC_{roc_period}": None,
        }

    result: dict[str, float | None] = {}

    # RSI
    try:
        rsi_series = compute_rsi(df, rsi_period)
        last_val = rsi_series.iloc[-1] if not rsi_series.empty else None
        result[rsi_series.name] = None if pd.isna(last_val) else float(last_val)
    except (ValueError, TypeError, IndexError) as e:  # M5: narrowed exception
        logger.warning("Failed to compute RSI", error=str(e))  # M4: error → warning
        result[f"RSI_{rsi_period}"] = None

    # StochRSI
    try:
        stochk, stochd = compute_stochrsi(df, stochrsi_k, stochrsi_d, stochrsi_period)
        for series in (stochk, stochd):
            last_val = series.iloc[-1] if not series.empty else None
            result[series.name] = None if pd.isna(last_val) else float(last_val)
    except (ValueError, TypeError, IndexError) as e:  # M5
        logger.warning("Failed to compute StochRSI", error=str(e))
        result[f"STOCHRSIk_{stochrsi_k}_{stochrsi_d}"] = None
        result[f"STOCHRSId_{stochrsi_k}_{stochrsi_d}"] = None

    # ROC
    try:
        roc_series = compute_roc(df, roc_period)
        last_val = roc_series.iloc[-1] if not roc_series.empty else None
        result[roc_series.name] = None if pd.isna(last_val) else float(last_val)
    except (ValueError, TypeError, IndexError) as e:  # M5
        logger.warning("Failed to compute ROC", error=str(e))
        result[f"ROC_{roc_period}"] = None

    return result