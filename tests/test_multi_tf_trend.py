"""
测试: multi_tf_trend.py

核心测试点:
- FAST 周期不影响方向判断
- PRIMARY + CONFIRM 同向 = STRONG
- 仅 PRIMARY = WEAK
- PRIMARY FLAT = FLAT
- 防漂移规则
"""

from __future__ import annotations

import pytest

from analysis.multi_tf_trend import (
    PRIMARY, CONFIRM, FAST,
    DIRECTION_LONG, DIRECTION_SHORT, DIRECTION_FLAT,
    STRENGTH_STRONG, STRENGTH_WEAK,
    infer_trend_direction,
    compute_tf_trends,
    get_consensus,
    get_fast_entry_bias,
    build_trend_summary,
    _is_nan,
)


class TestInferTrendDirection:
    """测试 infer_trend_direction 的方向推断逻辑。"""

    def test_ema_bullish_alignment(self):
        """多头 EMA 排列 → LONG"""
        indicators = {
            "EMA_9": 42200.0,
            "EMA_21": 42100.0,
            "EMA_55": 41800.0,
            "EMA_200": 41000.0,
            "close": 42250.0,
            "SMA_20": 42000.0,
            "MACD_hist": 15.0,
            "RSI_14": 58.0,
            "ADX_14": 30.0,
            "VWAP": 42100.0,
        }
        assert infer_trend_direction(indicators, "TRENDING") == DIRECTION_LONG

    def test_ema_bearish_alignment(self):
        """空头 EMA 排列 → SHORT"""
        indicators = {
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
        result = infer_trend_direction(indicators, "TRENDING")
        assert result == DIRECTION_SHORT

    def test_low_adx_ranging_returns_flat(self):
        """RANGING 制度 + 低 ADX → FLAT"""
        indicators = {
            "EMA_9": 42100.0,
            "EMA_21": 42050.0,
            "EMA_55": 42000.0,
            "close": 42100.0,
            "SMA_20": 42050.0,
            "MACD_hist": 5.0,
            "RSI_14": 52.0,
            "ADX_14": 15.0,
            "VWAP": 42050.0,
        }
        assert infer_trend_direction(indicators, "RANGING") == DIRECTION_FLAT

    def test_rsi_oversold_in_trend(self):
        """RSI 超卖 + TRENDING → LONG"""
        indicators = {
            "EMA_9": 42000.0,
            "EMA_21": 41950.0,
            "EMA_55": 41900.0,
            "close": 41980.0,
            "SMA_20": 41950.0,
            "MACD_hist": -2.0,
            "RSI_14": 28.0,
            "ADX_14": 28.0,
            "VWAP": 42000.0,
        }
        assert infer_trend_direction(indicators, "TRENDING") == DIRECTION_LONG

    def test_rsi_overbought(self):
        """RSI 严重超买 → SHORT（需 EMA 也看空配合）"""
        indicators = {
            "EMA_9": 43000.0,
            "EMA_21": 43100.0,  # EMA_9 < EMA_21 → 空头排列
            "EMA_55": 43200.0,   # EMA_21 < EMA_55 → 确认空头
            "EMA_200": 43500.0,  # EMA_55 < EMA_200 → 完全空头
            "close": 42900.0,
            "SMA_20": 43100.0,   # close < SMA → 看空
            "MACD_hist": -5.0,   # MACD 负值 → 看空
            "RSI_14": 82.0,       # 严重超买 → bearish_count + 2
            "ADX_14": 35.0,
            "VWAP": 43100.0,     # close < VWAP * 0.995 → 看空
        }
        assert infer_trend_direction(indicators, "TRENDING") == DIRECTION_SHORT

    def test_mixed_signals_returns_flat(self):
        """多空证据均衡 → FLAT"""
        indicators = {
            "EMA_9": 42100.0,
            "EMA_21": 42050.0,
            "EMA_55": 42100.0,
            "close": 42150.0,
            "SMA_20": 42100.0,
            "MACD_hist": 5.0,
            "RSI_14": 55.0,
            "ADX_14": 22.0,
            "VWAP": 42100.0,
        }
        result = infer_trend_direction(indicators, "UNKNOWN")
        assert result == DIRECTION_FLAT


class TestGetConsensus:
    """测试 get_consensus 的多周期共识逻辑。"""

    def test_primary_long_confirm_long_strong(self):
        """PRIMARY LONG + CONFIRM LONG = STRONG"""
        trends = {
            "1h": {"direction": "LONG"},
            "4h": {"direction": "LONG"},
            "1d": {"direction": "FLAT"},
            "5m": {"direction": "LONG"},
            "15m": {"direction": "SHORT"},
        }
        direction, strength = get_consensus(trends)
        assert direction == DIRECTION_LONG
        assert strength == STRENGTH_STRONG

    def test_primary_long_no_confirm_weak(self):
        """PRIMARY LONG + CONFIRM 无同向 = WEAK"""
        trends = {
            "1h": {"direction": "LONG"},
            "4h": {"direction": "FLAT"},
            "1d": {"direction": "SHORT"},
        }
        direction, strength = get_consensus(trends)
        assert direction == DIRECTION_LONG
        assert strength == STRENGTH_WEAK

    def test_primary_flat(self):
        """PRIMARY FLAT = FLAT"""
        trends = {
            "1h": {"direction": "FLAT"},
            "4h": {"direction": "LONG"},
            "1d": {"direction": "LONG"},
        }
        direction, strength = get_consensus(trends)
        assert direction == DIRECTION_FLAT
        assert strength == STRENGTH_WEAK

    def test_primary_short_with_confirm_strong(self):
        """PRIMARY SHORT + CONFIRM SHORT = STRONG"""
        trends = {
            "1h": {"direction": "SHORT"},
            "4h": {"direction": "SHORT"},
            "1d": {"direction": "FLAT"},
        }
        direction, strength = get_consensus(trends)
        assert direction == DIRECTION_SHORT
        assert strength == STRENGTH_STRONG

    def test_fast_cycles_do_not_affect_consensus(self):
        """
        防漂移测试：FAST 周期方向不应影响共识判断。
        即使 FAST 周期全部反向，共识仍由 PRIMARY + CONFIRM 决定。
        """
        trends = {
            "1h": {"direction": "LONG"},
            "4h": {"direction": "LONG"},
            "1d": {"direction": "LONG"},
            "5m": {"direction": "SHORT"},
            "15m": {"direction": "SHORT"},
        }
        direction, strength = get_consensus(trends)
        assert direction == DIRECTION_LONG
        assert strength == STRENGTH_STRONG

    def test_conservative_with_unknown(self):
        """PRIMARY FLAT → FLAT"""
        trends = {
            "1h": {"direction": "FLAT"},
            "4h": {"direction": "LONG"},
            "1d": {"direction": "SHORT"},
        }
        direction, strength = get_consensus(trends)
        assert direction == DIRECTION_FLAT


class TestGetFastEntryBias:
    """测试 FAST 周期入场偏向。"""

    def test_fast_aligned_with_consensus(self):
        """FAST 与共识方向一致 → 返回该方向"""
        trends = {
            "5m": {"direction": "LONG"},
            "15m": {"direction": "LONG"},
        }
        bias = get_fast_entry_bias(trends, "LONG")
        assert bias == "LONG"

    def test_fast_misaligned_returns_none(self):
        """FAST 与共识方向不一致 → None"""
        trends = {
            "5m": {"direction": "SHORT"},
            "15m": {"direction": "SHORT"},
        }
        bias = get_fast_entry_bias(trends, "LONG")
        assert bias is None

    def test_fast_partial_alignment(self):
        """FAST 部分与共识一致 → 仍返回偏向（半数以上）"""
        trends = {
            "5m": {"direction": "LONG"},
            "15m": {"direction": "FLAT"},
        }
        bias = get_fast_entry_bias(trends, "LONG")
        assert bias == "LONG"

    def test_flat_consensus_returns_none(self):
        """共识方向为 FLAT → None"""
        trends = {
            "5m": {"direction": "LONG"},
            "15m": {"direction": "SHORT"},
        }
        bias = get_fast_entry_bias(trends, "FLAT")
        assert bias is None

    def test_fast_does_not_change_direction(self):
        """
        防漂移测试：FAST 周期不能改变方向，
        只能确认入场时机（方向由 PRIMARY + CONFIRM 决定）。
        """
        trends = {
            "5m": {"direction": "SHORT"},
            "15m": {"direction": "SHORT"},
        }
        bias = get_fast_entry_bias(trends, "LONG")
        assert bias is None


class TestBuildTrendSummary:
    """测试 build_trend_summary 完整流程。"""

    def test_full_summary_structure(self):
        """验证返回结构完整性。"""
        tf_indicators = {
            "1h": {"EMA_9": 42100.0, "close": 42150.0, "SMA_20": 42000.0, "RSI_14": 58.0, "MACD_hist": 10.0, "ADX_14": 28.0, "VWAP": 42050.0},
            "4h": {"EMA_9": 41900.0, "close": 42000.0, "RSI_14": 55.0, "MACD_hist": -5.0, "ADX_14": 30.0},
            "1d": {"EMA_9": 41500.0, "close": 41800.0, "RSI_14": 52.0, "MACD_hist": 3.0, "ADX_14": 32.0},
        }
        summary = build_trend_summary(tf_indicators, "TRENDING")

        assert "consensus" in summary
        assert "direction" in summary["consensus"]
        assert "strength" in summary["consensus"]
        assert summary["primary"] == "1h"
        assert "confirm_timeframes" in summary
        assert "fast_timeframes" in summary
        assert "trends" in summary
        assert "regime" in summary
        assert summary["regime"] == "TRENDING"

    def test_empty_indicators(self):
        """空指标字典 → 返回 FLAT。"""
        summary = build_trend_summary({}, "UNKNOWN")
        assert summary["consensus"]["direction"] == DIRECTION_FLAT


class TestIsNan:
    """测试 _is_nan 辅助函数。"""

    def test_nan_value(self):
        import math
        assert _is_nan(math.nan) is True

    def test_normal_value(self):
        assert _is_nan(42.0) is False
        assert _is_nan(0.0) is False
        assert _is_nan(-1.0) is False
