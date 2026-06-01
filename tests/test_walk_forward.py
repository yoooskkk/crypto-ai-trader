"""
Walk-Forward 验证引擎测试。
覆盖：方向解析、窗口回测、指标计算、窗口生成、汇总聚合、完整流程。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from validation.walk_forward import (
    WalkForwardConfig,
    WalkForwardEngine,
    _compute_metrics,
    _date_str_to_ts,
    _parse_signal_direction,
    _run_window_backtest,
    _ts_to_date,
    _WindowBacktestResult,
)
from validation.output_schema import BacktestResult, WalkForwardResult


# ─── 辅助函数 ─────────────────────────────────────

def _make_price_df(
    n: int = 100,
    start_ts: int = 1700000000000,
    interval_ms: int = 3600000,  # 1h
    base_price: float = 42000.0,
) -> pd.DataFrame:
    """生成模拟 OHLCV 价格 DataFrame。"""
    ts_list = [start_ts + i * interval_ms for i in range(n)]
    closes = [
        base_price * (1 + 0.001 * np.sin(i * 0.3) + 0.0002 * (i % 7 - 3))
        for i in range(n)
    ]
    return pd.DataFrame({
        "ts": ts_list,
        "open": [c * 0.999 for c in closes],
        "high": [c * 1.002 for c in closes],
        "low": [c * 0.998 for c in closes],
        "close": closes,
        "volume": [100.0 + i for i in range(n)],
    })


def _make_signal_df(
    n: int = 20,
    start_ts: int = 1700000000000,
    interval_ms: int = 3600000,
    direction: str = "LONG",
) -> pd.DataFrame:
    """生成模拟信号 DataFrame。"""
    ts_list = [start_ts + i * interval_ms for i in range(n)]
    return pd.DataFrame({
        "ts": ts_list,
        "direction": [direction] * n,
        "confidence": [0.8] * n,
    })


# ═══════════════════════════════════════════════════
# 1. _parse_signal_direction
# ═══════════════════════════════════════════════════

class TestParseSignalDirection:
    """方向解析函数测试。"""

    def test_long(self):
        assert _parse_signal_direction("LONG") == 1

    def test_short(self):
        assert _parse_signal_direction("SHORT") == -1

    def test_flat(self):
        assert _parse_signal_direction("FLAT") == 0

    def test_case_insensitive(self):
        assert _parse_signal_direction("long") == 1
        assert _parse_signal_direction("Short") == -1
        assert _parse_signal_direction("Flat") == 0

    def test_unknown_defaults_to_flat(self):
        assert _parse_signal_direction("HOLD") == 0
        assert _parse_signal_direction("") == 0


# ═══════════════════════════════════════════════════
# 2. _run_window_backtest
# ═══════════════════════════════════════════════════

class TestRunWindowBacktest:
    """窗口回测函数测试。"""

    def test_empty_prices_returns_no_trades(self):
        prices = pd.DataFrame()
        signals = _make_signal_df(5)
        result = _run_window_backtest(prices, signals, ts_col="ts")
        assert result.trades == 0

    def test_empty_signals_returns_no_trades(self):
        prices = _make_price_df(100)
        signals = pd.DataFrame()
        result = _run_window_backtest(prices, signals, ts_col="ts")
        assert result.trades == 0

    def test_long_signals_generate_trades(self):
        prices = _make_price_df(50)
        signals = _make_signal_df(10, direction="LONG")
        result = _run_window_backtest(prices, signals, ts_col="ts")
        assert result.trades > 0
        assert len(result.returns) == result.trades
        assert len(result.equity_curve) >= 2

    def test_short_signals_generate_trades(self):
        prices = _make_price_df(50)
        signals = _make_signal_df(10, direction="SHORT")
        result = _run_window_backtest(prices, signals, ts_col="ts")
        assert result.trades > 0

    def test_alternating_signals(self):
        """交替 LONG/SHORT 信号应产生多次交易。"""
        prices = _make_price_df(100)
        ts_list = [1700000000000 + i * 3600000 for i in range(10)]
        directions = ["LONG", "SHORT"] * 5
        signals = pd.DataFrame({"ts": ts_list, "direction": directions})
        result = _run_window_backtest(prices, signals, ts_col="ts")
        # 每次切换生成一次平仓 + 一次开仓，但平仓时算一次 trade
        assert result.trades >= 5
        # 最终应处于 SHORT 方向，窗口结束时强平
        assert result.trades <= 11  # 10 次信号变化 + 1 次终了强平

    def test_all_flat_signals_no_trades(self):
        prices = _make_price_df(50)
        signals = _make_signal_df(10, direction="FLAT")
        result = _run_window_backtest(prices, signals, ts_col="ts")
        assert result.trades == 0

    def test_forces_close_at_window_end(self):
        """窗口结束时应强平最后持仓。"""
        prices = _make_price_df(30)
        signals = _make_signal_df(5, direction="LONG", start_ts=1700000000000)
        result = _run_window_backtest(prices, signals, ts_col="ts")
        # 开仓 1 次（第一个 LONG 信号），窗口结束时强平
        assert result.trades >= 1
        assert len(result.equity_curve) >= 2

    def test_custom_price_col(self):
        """应支持自定义价格列名。"""
        prices = _make_price_df(50)
        prices["my_price"] = prices["close"] * 1.01
        signals = _make_signal_df(5, direction="LONG")
        result = _run_window_backtest(prices, signals, ts_col="ts", price_col="my_price")
        assert result.trades > 0


# ═══════════════════════════════════════════════════
# 3. _compute_metrics
# ═══════════════════════════════════════════════════

class TestComputeMetrics:
    """指标计算函数测试。"""

    def test_no_trades_returns_none(self):
        result = _WindowBacktestResult(trades=0)
        assert _compute_metrics(result) is None

    def test_all_wins(self):
        """全部盈利的交易。"""
        result = _WindowBacktestResult(
            trades=5,
            wins=5,
            returns=[0.01, 0.02, 0.015, 0.03, 0.005],
            equity_curve=[1.0, 1.01, 1.0302, 1.04565, 1.07702, 1.08241],
        )
        metrics = _compute_metrics(result)
        assert metrics is not None
        assert metrics.total_trades == 5
        assert metrics.win_rate == 1.0
        assert metrics.avg_trade_pct == pytest.approx(1.6, rel=0.1)

    def test_mixed_wins_and_losses(self):
        """混合盈亏。"""
        result = _WindowBacktestResult(
            trades=4,
            wins=2,
            returns=[0.02, -0.01, 0.03, -0.02],
            equity_curve=[1.0, 1.02, 1.0098, 1.04009, 1.01929],
        )
        metrics = _compute_metrics(result)
        assert metrics is not None
        assert metrics.total_trades == 4
        assert metrics.win_rate == 0.5

    def test_sharpe_positive_for_profitable(self):
        """正收益应有正夏普比率。"""
        result = _WindowBacktestResult(
            trades=10,
            wins=8,
            returns=[0.005] * 8 + [-0.003] * 2,
            equity_curve=[1.0] + [1.0 + i * 0.005 for i in range(1, 9)] + [1.037, 1.034],
        )
        metrics = _compute_metrics(result)
        assert metrics is not None
        assert metrics.sharpe > 0

    def test_max_drawdown_non_positive(self):
        """最大回撤应为非正数。"""
        result = _WindowBacktestResult(
            trades=6,
            wins=3,
            returns=[0.02, -0.05, 0.03, -0.04, 0.01, 0.02],
            equity_curve=[1.0, 1.02, 0.969, 0.99807, 0.95815, 0.96773, 0.98708],
        )
        metrics = _compute_metrics(result)
        assert metrics is not None
        assert metrics.max_drawdown <= 0.0


# ═══════════════════════════════════════════════════
# 4. WalkForwardEngine._generate_windows
# ═══════════════════════════════════════════════════

class TestGenerateWindows:
    """窗口生成测试。"""

    def test_generates_correct_number_of_windows(self):
        """给定数据范围应生成正确数量的窗口。"""
        engine = WalkForwardEngine(WalkForwardConfig(
            train_days=30, validate_days=10, step_days=10,
        ))
        # 200 天数据 -> floor((200-30-10)/10) + 1 = 17 个窗口
        min_ts = 1700000000000
        max_ts = min_ts + 200 * 86400000
        windows = engine._generate_windows(min_ts, max_ts)
        assert len(windows) == 17

    def test_window_boundaries(self):
        """窗口边界应正确。"""
        engine = WalkForwardEngine(WalkForwardConfig(
            train_days=30, validate_days=10, step_days=10,
        ))
        min_ts = 1700000000000
        max_ts = min_ts + 200 * 86400000
        windows = engine._generate_windows(min_ts, max_ts)

        # 第一个窗口
        first = windows[0]
        train_len = first[1] - first[0]
        val_len = first[3] - first[2]
        assert train_len == 30 * 86400000
        assert val_len == 10 * 86400000
        assert first[1] == first[2]  # train_end == val_start

        # 第二个窗口应步进 step_days
        second = windows[1]
        assert second[0] - first[0] == 10 * 86400000

    def test_insufficient_data_returns_empty(self):
        """数据不够一个窗口时应返回空。"""
        engine = WalkForwardEngine(WalkForwardConfig(
            train_days=90, validate_days=30, step_days=30,
        ))
        min_ts = 1700000000000
        max_ts = min_ts + 60 * 86400000  # 仅 60 天
        windows = engine._generate_windows(min_ts, max_ts)
        assert len(windows) == 0

    def test_custom_config(self):
        """自定义配置应正确影响窗口数量。"""
        engine = WalkForwardEngine(WalkForwardConfig(
            train_days=10, validate_days=5, step_days=5,
        ))
        min_ts = 1700000000000
        max_ts = min_ts + 100 * 86400000  # 100 天
        windows = engine._generate_windows(min_ts, max_ts)
        # floor((100-10-5)/5) + 1 = 18
        assert len(windows) == 18


# ═══════════════════════════════════════════════════
# 5. WalkForwardEngine.run 完整流程
# ═══════════════════════════════════════════════════

class TestWalkForwardEngineRun:
    """WalkForwardEngine.run 端到端测试。"""

    def test_with_sufficient_data_returns_result(self):
        """足够的数据应返回 WalkForwardResult。"""
        engine = WalkForwardEngine(WalkForwardConfig(
            train_days=10, validate_days=5, step_days=5,
            min_trades=1,
        ))
        # 信号需覆盖 15+ 天（train=10 + validate=5）
        prices = _make_price_df(2000, start_ts=1700000000000, interval_ms=3600000)
        signals = _make_signal_df(1000, start_ts=1700000000000, interval_ms=3600000, direction="LONG")
        result = engine.run(prices, signals, symbol="BTCUSDT")
        assert isinstance(result, WalkForwardResult)
        assert len(result.windows) > 0
        assert result.sharpe_variance >= 0.0
        assert isinstance(result.robust, bool)

    def test_empty_data_returns_empty_result(self):
        """空数据应返回空 WalkForwardResult。"""
        engine = WalkForwardEngine()
        prices = pd.DataFrame({"ts": [], "close": []})  # 有列但无行
        signals = pd.DataFrame({"ts": [], "direction": []})
        result = engine.run(prices, signals)
        assert len(result.windows) == 0
        assert result.robust is False

    def test_insufficient_data_returns_empty(self):
        """数据太短应返回空。"""
        engine = WalkForwardEngine(WalkForwardConfig(
            train_days=90, validate_days=30, step_days=30,
        ))
        prices = _make_price_df(10)
        signals = _make_signal_df(5)
        result = engine.run(prices, signals)
        assert len(result.windows) == 0

    def test_custom_ts_col(self):
        """自定义时间戳列名。"""
        engine = WalkForwardEngine(WalkForwardConfig(
            train_days=10, validate_days=5, step_days=5,
            min_trades=1,
        ))
        prices = _make_price_df(2000)
        prices.rename(columns={"ts": "timestamp"}, inplace=True)
        signals = _make_signal_df(1000)
        signals.rename(columns={"ts": "timestamp"}, inplace=True)
        result = engine.run(prices, signals, ts_col="timestamp")
        assert len(result.windows) > 0

    def test_min_trades_filter(self):
        """低于 min_trades 的窗口应被过滤。"""
        engine = WalkForwardEngine(WalkForwardConfig(
            train_days=10, validate_days=5, step_days=5,
            min_trades=100,  # 不可能满足
        ))
        prices = _make_price_df(500)
        signals = _make_signal_df(10, start_ts=1700000000000)
        result = engine.run(prices, signals)
        # 信号太少，所有窗口都会被过滤
        assert len(result.windows) == 0


# ═══════════════════════════════════════════════════
# 6. _aggregate 汇总
# ═══════════════════════════════════════════════════

class TestAggregate:
    """汇总函数测试。"""

    def test_empty_list_returns_empty_result(self):
        result = WalkForwardEngine._aggregate([])
        assert len(result.windows) == 0
        assert result.avg_sharpe == 0.0
        assert result.robust is False

    def test_single_window(self):
        results = [
            BacktestResult(
                total_trades=10, win_rate=0.5, sharpe=1.2,
                max_drawdown=-0.1, profit_factor=1.5, avg_trade_pct=0.5,
                strategy="test", start_date="2024-01-01", end_date="2024-01-31",
            ),
        ]
        result = WalkForwardEngine._aggregate(results)
        assert result.avg_sharpe == 1.2
        assert result.sharpe_variance == 0.0  # 只有 1 个窗口时方差为 0
        assert result.robust is True

    def test_multiple_windows(self):
        results = [
            BacktestResult(
                total_trades=10, win_rate=0.5, sharpe=1.5,
                max_drawdown=-0.1, profit_factor=1.5, avg_trade_pct=0.5,
                strategy="w0", start_date="", end_date="",
            ),
            BacktestResult(
                total_trades=8, win_rate=0.6, sharpe=1.0,
                max_drawdown=-0.15, profit_factor=1.2, avg_trade_pct=0.3,
                strategy="w1", start_date="", end_date="",
            ),
            BacktestResult(
                total_trades=12, win_rate=0.55, sharpe=0.8,
                max_drawdown=-0.2, profit_factor=1.1, avg_trade_pct=0.2,
                strategy="w2", start_date="", end_date="",
            ),
        ]
        result = WalkForwardEngine._aggregate(results)
        assert result.avg_sharpe == pytest.approx(1.1, rel=0.01)
        assert result.sharpe_variance > 0
        assert result.robust is True  # variance < 2.0


# ═══════════════════════════════════════════════════
# 7. 辅助函数
# ═══════════════════════════════════════════════════

class TestHelpers:
    """辅助函数测试。"""

    def test_ts_to_date(self):
        date_str = _ts_to_date(1700000000000)
        assert isinstance(date_str, str)
        assert len(date_str) == 10
        assert date_str == "2023-11-14"

    def test_date_str_to_ts(self):
        ts = _date_str_to_ts("2023-11-14")
        assert isinstance(ts, int)
        assert ts > 0
