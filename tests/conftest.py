"""
测试共享配置和 fixtures。
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# ─── numba 兼容性 shim（Python 3.14+ 暂不支持 numba） ─────
# 在 pandas_ta 导入前，将 numba 替换为无操作 mock
if "numba" not in sys.modules:
    import types

    _numba = types.ModuleType("numba")

    def _njit(func=None, *args, **kwargs):
        """无操作装饰器"""
        if func is not None:
            return func
        def decorator(f):
            return f
        return decorator

    _numba.njit = _njit
    _numba.generated_jit = _njit
    _numba.prange = range
    _numba.__version__ = "0.61.2-shim"

    sys.modules["numba"] = _numba

    # 也 mock numba.core 和 numba.typed
    for _sub in ("core", "typed", "experimental"):
        _sub_mod = types.ModuleType(f"numba.{_sub}")
        _sub_mod.__path__ = []
        sys.modules[f"numba.{_sub}"] = _sub_mod


@pytest.fixture
def sample_bullish_indicators_1h() -> dict:
    """1h 看多指标样本。"""
    return {
        "EMA_9": 42100.0,
        "EMA_21": 42000.0,
        "EMA_55": 41800.0,
        "EMA_200": 41000.0,
        "close": 42150.0,
        "SMA_20": 42000.0,
        "MACD_hist": 15.0,
        "RSI_14": 58.0,
        "ADX_14": 28.0,
        "VWAP": 42050.0,
    }


@pytest.fixture
def sample_bearish_indicators_1h() -> dict:
    """1h 看空指标样本。"""
    return {
        "EMA_9": 41800.0,
        "EMA_21": 41900.0,
        "EMA_55": 42100.0,
        "EMA_200": 42500.0,
        "close": 41750.0,
        "SMA_20": 42000.0,
        "MACD_hist": -15.0,
        "RSI_14": 35.0,
        "ADX_14": 28.0,
        "VWAP": 42100.0,
    }


@pytest.fixture
def sample_multi_tf_indicators() -> dict[str, dict]:
    """多周期指标样本。"""
    return {
        "1h": {
            "EMA_9": 42100.0, "close": 42150.0, "SMA_20": 42000.0,
            "RSI_14": 58.0, "MACD_hist": 10.0, "ADX_14": 28.0, "VWAP": 42050.0,
        },
        "4h": {
            "EMA_9": 41900.0, "close": 42000.0, "RSI_14": 55.0,
            "MACD_hist": -5.0, "ADX_14": 30.0,
        },
        "1d": {
            "EMA_9": 41500.0, "close": 41800.0, "RSI_14": 52.0,
            "MACD_hist": 3.0, "ADX_14": 32.0,
        },
    }


@pytest.fixture
def sample_trending_regime() -> dict:
    """TRENDING 制度信号。"""
    return {
        "symbol": "BTCUSDT",
        "ts": 1700000000000,
        "regime": "TRENDING",
        "confidence": 0.85,
        "method": "rule_based",
        "adx": 28.5,
        "bb_width": 0.042,
    }


@pytest.fixture
def sample_ranging_regime() -> dict:
    """RANGING 制度信号。"""
    return {
        "symbol": "BTCUSDT",
        "ts": 1700000000000,
        "regime": "RANGING",
        "confidence": 0.75,
        "method": "rule_based",
        "adx": 15.0,
        "bb_width": 0.015,
    }
