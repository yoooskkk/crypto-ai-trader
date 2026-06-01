"""Tests for analysis/pnl_attribution.py — PnL 归因分析"""
from __future__ import annotations

import numpy as np
import pytest

from analysis.pnl_attribution import PnLAttributor, TradeRecord, AttributionReport


class TestPnLAttributor:
    """PnL 归因分析测试"""

    @pytest.fixture
    def attributor(self) -> PnLAttributor:
        return PnLAttributor()

    @pytest.fixture
    def sample_trades(self) -> list[TradeRecord]:
        rng = np.random.default_rng(42)
        trades = []
        for i in range(50):
            direction = "LONG" if rng.random() > 0.5 else "SHORT"
            entry = 100.0 + rng.normal(0, 5)
            pnl_pct = rng.normal(0.001, 0.02)
            exit_price = entry * (1 + pnl_pct) if direction == "LONG" else entry * (1 - pnl_pct)
            trades.append(TradeRecord(
                symbol="BTC/USDT",
                direction=direction,
                entry_price=entry,
                exit_price=exit_price,
                volume=0.1,
                entry_time=1000 + i * 1000,
                exit_time=2000 + i * 1000,
                pnl=(exit_price - entry) * 0.1 if direction == "LONG" else (entry - exit_price) * 0.1,
                pnl_pct=pnl_pct,
                confidence=0.7 + rng.random() * 0.3,
                signal_score=0.5 + rng.random() * 0.5,
                regime=rng.choice(["TRENDING", "RANGING", "HIGH_VOLATILITY"]),
                factors={"RSI_14": 50 + rng.random() * 30, "ADX_14": 25 + rng.random() * 15},
            ))
        return trades

    def test_empty_trades(self, attributor):
        report = attributor.analyze([])
        assert report.total_trades == 0
        assert report.total_pnl == 0.0

    def test_single_trade(self, attributor):
        trade = TradeRecord(
            symbol="BTC", direction="LONG",
            entry_price=100, exit_price=110, volume=1,
            entry_time=1000, exit_time=2000,
            pnl=10.0, pnl_pct=0.10,
            confidence=0.8, signal_score=0.9, regime="TRENDING",
        )
        report = attributor.analyze([trade])
        assert report.total_trades == 1
        assert report.total_pnl == pytest.approx(10.0)
        assert report.win_trades == 1

    def test_multiple_trades(self, attributor, sample_trades):
        report = attributor.analyze(sample_trades)
        assert report.total_trades == 50
        assert report.win_trades + report.loss_trades == 50
        assert report.win_rate > 0

    def test_direction_grouping(self, attributor, sample_trades):
        report = attributor.analyze(sample_trades)
        assert "LONG" in report.by_direction or "SHORT" in report.by_direction
        total_count = sum(
            info["count"] for info in report.by_direction.values()
        )
        assert total_count == 50

    def test_regime_grouping(self, attributor, sample_trades):
        report = attributor.analyze(sample_trades)
        assert len(report.by_regime) > 0

    def test_sharpe_ratio(self, attributor, sample_trades):
        report = attributor.analyze(sample_trades)
        assert isinstance(report.sharpe, float)

    def test_sortino_ratio(self, attributor, sample_trades):
        report = attributor.analyze(sample_trades)
        assert isinstance(report.sortino, float)

    def test_max_drawdown(self, attributor, sample_trades):
        report = attributor.analyze(sample_trades)
        assert 0.0 <= report.max_drawdown < 1.0

    def test_factor_correlation(self, attributor, sample_trades):
        report = attributor.analyze(sample_trades)
        assert isinstance(report.by_factor_corr, dict)

    def test_summary_text(self, attributor, sample_trades):
        report = attributor.analyze(sample_trades)
        text = report.summary_text()
        assert "PnL Attribution Report" in text
        assert "Total PnL" in text

    def test_identical_trade_classification(self, attributor):
        """多笔完全相同交易 → win_rate 准确"""
        trades = [
            TradeRecord(
                symbol="BTC", direction="LONG",
                entry_price=100, exit_price=110, volume=1,
                entry_time=1000, exit_time=2000,
                pnl=10.0, pnl_pct=0.10,
                confidence=0.8, signal_score=0.9, regime="TRENDING",
            ),
            TradeRecord(
                symbol="BTC", direction="LONG",
                entry_price=100, exit_price=90, volume=1,
                entry_time=3000, exit_time=4000,
                pnl=-10.0, pnl_pct=-0.10,
                confidence=0.8, signal_score=0.9, regime="TRENDING",
            ),
        ]
        report = attributor.analyze(trades)
        assert report.win_trades == 1
        assert report.loss_trades == 1
        assert report.win_rate == 0.5
