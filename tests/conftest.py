"""
测试共享配置和 fixtures。
"""

from __future__ import annotations

import pytest


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
