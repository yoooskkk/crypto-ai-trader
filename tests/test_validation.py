"""
验证层模块联合测试：
  - factor_decay.py：因子衰减监控
  - oos_test.py：OOS 封存测试
  - paper_trading_parallel.py：模拟盘/回测对比
"""
from __future__ import annotations

import pandas as pd
import pytest

from validation.factor_decay import (
    FactorDecayConfig,
    FactorDecayMonitor,
    FactorDecayReport,
)
from validation.oos_test import OOSTestConfig, OOSTestEngine, OOSTestReport
from validation.paper_trading_parallel import (
    ParallelComparisonConfig,
    ParallelComparisonReport,
    PaperTradingParallel,
)


# ═══════════════════════════════════════════════════
# 1. FactorDecayMonitor
# ═══════════════════════════════════════════════════

class TestFactorDecayMonitor:
    """因子衰减监控测试。"""

    def test_insufficient_data_returns_no_decay(self):
        """数据点不足 2 个时不应标记衰减。"""
        monitor = FactorDecayMonitor()
        report = monitor.analyze("test", [0.05])
        assert report.is_decaying is False
        assert report.ic_mean == 0.05

    def test_empty_data(self):
        """空数据不应标记衰减。"""
        monitor = FactorDecayMonitor()
        report = monitor.analyze("test", [])
        assert report.is_decaying is False
        assert report.ic_mean == 0.0

    def test_high_ic_no_decay(self):
        """高 IC 均值 + 稳定趋势不应标记衰减。"""
        monitor = FactorDecayMonitor(
            FactorDecayConfig(ic_threshold=0.02, half_life_max=80)
        )
        # 100 个光滑递增的数据点 → 高自相关 → 半衰期长
        base = 0.03
        ics = [base + i * 0.0005 for i in range(100)]
        report = monitor.analyze("momentum", ics)
        assert report.is_decaying is False
        assert report.ic_mean >= 0.05

    def test_low_ic_triggers_decay(self):
        """IC 均值低于阈值应标记衰减。"""
        monitor = FactorDecayMonitor(FactorDecayConfig(ic_threshold=0.03))
        ics = [0.01, 0.005, 0.02, 0.015, 0.01, 0.008, 0.012, 0.009]
        report = monitor.analyze("momentum", ics)
        assert report.is_decaying is True
        assert "低于阈值" in report.alert_message

    def test_negative_slope_triggers_decay(self):
        """持续负斜率的 IC 应标记衰减。"""
        monitor = FactorDecayMonitor(FactorDecayConfig(
            ic_threshold=0.0,  # 不触发均值告警
            slope_threshold=-0.001,
        ))
        # 持续下降的 IC
        ics = [0.08, 0.07, 0.06, 0.05, 0.04, 0.03, 0.02, 0.01, 0.005, 0.001]
        report = monitor.analyze("momentum", ics)
        assert report.is_decaying is True
        assert report.ic_trend_slope < 0
        assert "斜率" in report.alert_message

    def test_rising_ic_no_decay(self):
        """上升 IC 不应标记衰减。"""
        monitor = FactorDecayMonitor()
        ics = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]
        report = monitor.analyze("momentum", ics)
        assert report.ic_trend_slope > 0

    def test_moving_average(self):
        """移动平均应正确计算。"""
        ma = FactorDecayMonitor._moving_average(
            [0.1, 0.2, 0.3, 0.4, 0.5], window=3,
        )
        assert ma == pytest.approx(0.4)  # mean of [0.3, 0.4, 0.5]

    def test_half_life_computation(self):
        """半衰期应返回正整数值。"""
        # 强自相关 → 半衰期长
        ics = [0.05, 0.051, 0.049, 0.052, 0.048, 0.05, 0.051, 0.049]
        hl = FactorDecayMonitor._compute_half_life(ics)
        assert isinstance(hl, int)
        assert hl > 0

    def test_report_fields(self):
        """报告字段应完整。"""
        monitor = FactorDecayMonitor()
        report = monitor.analyze("test_factor", [0.04, 0.05, 0.03, 0.06, 0.04])
        assert isinstance(report, FactorDecayReport)
        assert report.factor_name == "test_factor"
        assert len(report.ic_values) == 5
        assert 0.0 <= report.ic_std <= 1.0


# ═══════════════════════════════════════════════════
# 2. OOSTestEngine
# ═══════════════════════════════════════════════════

class TestOOSTestEngine:
    """OOS 封存测试引擎测试。"""

    def _make_prices(self, n: int = 30) -> pd.DataFrame:
        ts = [1700000000000 + i * 3600000 for i in range(n)]
        closes = [42000.0 + i * 10 for i in range(n)]
        return pd.DataFrame({"ts": ts, "close": closes})

    def _make_signals(self, n: int = 10, direction: str = "LONG") -> pd.DataFrame:
        ts = [1700000000000 + i * 3600000 for i in range(n)]
        return pd.DataFrame({"ts": ts, "direction": [direction] * n})

    def test_empty_data_returns_invalid(self):
        """空数据应返回无效报告。"""
        engine = OOSTestEngine(OOSTestConfig(enforce_single_use=False))
        prices = pd.DataFrame({"ts": [], "close": []})
        signals = pd.DataFrame({"ts": [], "direction": []})
        report = engine.run(prices, signals, symbol="BTCUSDT")
        assert report.is_valid is False
        assert report.was_already_used is False

    def test_sufficient_data_returns_valid(self):
        """足够的数据应返回有效报告。"""
        engine = OOSTestEngine(OOSTestConfig(
            min_trades=1, enforce_single_use=False,
        ))
        prices = self._make_prices(100)
        signals = self._make_signals(20)
        report = engine.run(prices, signals, symbol="BTCUSDT")
        assert report.is_valid is True
        assert report.total_trades > 0
        assert report.symbol == "BTCUSDT"

    def test_lock_file_prevents_reuse(self):
        """标记文件应阻止重复使用（铁律 #3）。"""
        # 首次使用 — 成功
        engine1 = OOSTestEngine(OOSTestConfig(
            min_trades=1, enforce_single_use=True,
        ))
        engine1.reset_lock()  # 确保初始状态干净
        prices = self._make_prices(100)
        signals = self._make_signals(20)
        report1 = engine1.run(prices, signals)
        assert report1.is_valid is True

        # 第二次使用 — 应被阻止
        engine2 = OOSTestEngine(OOSTestConfig(
            min_trades=1, enforce_single_use=True,
        ))
        report2 = engine2.run(prices, signals)
        assert report2.was_already_used is True
        assert report2.is_valid is False
        assert "已被使用过" in report2.message

        # 清理标记
        engine2.reset_lock()

    def test_lock_disabled_allows_reuse(self):
        """关闭 enforce_single_use 应允许多次使用。"""
        engine = OOSTestEngine(OOSTestConfig(
            min_trades=1, enforce_single_use=False,
        ))
        engine.reset_lock()
        prices = self._make_prices(100)
        signals = self._make_signals(20)

        report1 = engine.run(prices, signals)
        assert report1.is_valid is True

        report2 = engine.run(prices, signals)
        assert report2.is_valid is True  # 不阻止

    def test_reset_lock(self):
        """reset_lock 应正确清除标记。"""
        engine = OOSTestEngine(OOSTestConfig(enforce_single_use=False))
        engine.reset_lock()  # 清理前一个测试可能留下的锁
        assert engine._is_already_used() is False
        # 标记为已用
        engine._mark_used()
        assert engine._is_already_used() is True
        # 重置
        engine.reset_lock()
        assert engine._is_already_used() is False

    def test_insufficient_trades(self):
        """交易次数不足应标记无效。"""
        engine = OOSTestEngine(OOSTestConfig(
            min_trades=100, enforce_single_use=False,
        ))
        prices = self._make_prices(30)
        signals = self._make_signals(5)
        report = engine.run(prices, signals)
        assert report.is_valid is False
        assert "低于最小要求" in report.message


# ═══════════════════════════════════════════════════
# 3. PaperTradingParallel
# ═══════════════════════════════════════════════════

class TestPaperTradingParallel:
    """模拟盘/回测并行对比测试。"""

    def _make_signals(
        self,
        n: int = 20,
        direction: str = "LONG",
        confidence: float = 0.8,
        start_ts: int = 1700000000000,
    ) -> pd.DataFrame:
        ts = [start_ts + i * 3600000 for i in range(n)]
        return pd.DataFrame({
            "ts": ts,
            "direction": [direction] * n,
            "confidence": [confidence] * n,
        })

    def test_empty_signals(self):
        """空信号应返回空报告。"""
        comparator = PaperTradingParallel()
        bt = pd.DataFrame()
        pt = pd.DataFrame()
        report = comparator.compare(bt, pt)
        assert report.total_signals == 0
        assert "空" in report.summary

    def test_identical_signals_perfect_agreement(self):
        """完全相同的信号应有 100% 方向一致率。"""
        comparator = PaperTradingParallel()
        bt = self._make_signals(20, "LONG")
        pt = self._make_signals(20, "LONG")
        report = comparator.compare(bt, pt)
        assert report.direction_agreement_pct == 1.0
        assert report.total_signals >= 19  # merge_asof 可能丢失边界

    def test_opposite_signals_low_agreement(self):
        """完全相反的方向应有 0% 一致率。"""
        comparator = PaperTradingParallel()
        bt = self._make_signals(20, "LONG")
        pt = self._make_signals(20, "SHORT")
        report = comparator.compare(bt, pt)
        assert report.direction_agreement_pct < 0.1
        assert report.direction_bias in ("SHORT", "NEUTRAL")

    def test_confidence_correlation(self):
        """置信度相关系数应合理。"""
        comparator = PaperTradingParallel()
        bt_ts = [1700000000000 + i * 3600000 for i in range(20)]
        pt_ts = [1700000000000 + i * 3600000 for i in range(20)]
        bt = pd.DataFrame({
            "ts": bt_ts,
            "direction": ["LONG"] * 20,
            "confidence": [0.8 + i * 0.01 for i in range(20)],
        })
        pt = pd.DataFrame({
            "ts": pt_ts,
            "direction": ["LONG"] * 20,
            "confidence": [0.75 + i * 0.01 for i in range(20)],
        })
        report = comparator.compare(bt, pt)
        assert report.confidence_correlation > 0.5  # 高度相关

    def test_signal_freq_ratio(self):
        """信号频次比应正确反映信号数量差异。"""
        comparator = PaperTradingParallel()
        bt = self._make_signals(10, "LONG")
        pt = self._make_signals(20, "LONG")
        report = comparator.compare(bt, pt)
        assert report.signal_freq_ratio == pytest.approx(2.0, rel=0.2)

    def test_normalize_direction(self):
        """方向标准化应正确处理各种输入格式。"""
        from validation.paper_trading_parallel import _normalize_direction
        assert _normalize_direction("LONG") == "LONG"
        assert _normalize_direction("short") == "SHORT"
        assert _normalize_direction("Flat") == "FLAT"
        assert _normalize_direction(1) == "LONG"
        assert _normalize_direction(-1) == "SHORT"
        assert _normalize_direction(0) == "FLAT"
