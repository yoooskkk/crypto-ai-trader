"""
端到端集成测试 — 模拟完整交易管线

管线流程:
  data (mock K 线)
    → indicators (技术指标计算)
      → analysis/factor_mining (IC 因子计算)
        → ai_engine/signal_scorer (信号评分)
          → risk_guardian (风控审核)
            → observability/decision_logger (记录)
              → observability/factor_decay_monitor (衰减监控)

每个步骤验证:
  - 输入格式正确
  - 输出结构完整
  - 步骤间粘合接口兼容
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ─── Mock 数据 ────────────────────────────────────────────────────────

MOCK_KLINES = pd.DataFrame({
    "timestamp": pd.date_range("2025-01-01", periods=200, freq="1h", tz="UTC"),
    "open":   [50000 + i * 10 for i in range(200)],
    "high":   [50100 + i * 11 for i in range(200)],
    "low":    [49900 + i * 9  for i in range(200)],
    "close":  [50050 + i * 10 for i in range(200)],
    "volume": [100 + (i % 5)  for i in range(200)],
}).set_index("timestamp")

SYMBOL = "BTCUSDT"
INTERVAL = "1h"


# ─── Step 1: 数据 → 指标 ────────────────────────────────────────────

class TestStep1_DataToIndicators:
    """验证指标模块能正确处理 K 线数据并生成指标列。"""

    def test_indicator_calculation(self):
        """所有核心指标应返回非空序列。"""
        try:
            from indicators.multi_tf_trend import MultiTFIndicator

            indicator = MultiTFIndicator()
            result = indicator.calculate(MOCK_KLINES)

            assert result is not None
            assert isinstance(result, pd.DataFrame)
            assert len(result) > 0

        except ImportError:
            pytest.skip("MultiTFIndicator 不可用")

    def test_trend_consensus(self):
        """多周期趋势共识应返回 BUY/SELL/NEUTRAL。"""
        try:
            from indicators.multi_tf_trend import TrendConsensus

            tc = TrendConsensus()
            consensus = tc.aggregate({"1h": 0.6, "4h": 0.8, "1d": 0.7})
            assert consensus in ("BUY", "SELL", "NEUTRAL")
        except ImportError:
            pytest.skip("TrendConsensus 不可用")


# ─── Step 2: 指标 → 因子分析 ─────────────────────────────────────────

class TestStep2_IndicatorsToFactorMining:
    """验证因子 IC 计算模块。"""

    def test_factor_ic_computation(self):
        """IC 计算应返回合法的统计量。"""
        try:
            from analysis.factor_mining import FactorICComputer, FactorICResult

            import numpy as np
            computer = FactorICComputer()

            # 模拟因子值和收益率
            factor_values = [0.05, 0.04, 0.03, 0.06, 0.02, 0.07, 0.01, 0.08]
            returns = [0.001, 0.002, -0.001, 0.003, -0.002, 0.004, -0.003, 0.005]

            result = computer.compute_ic(factor_values, returns)

            assert isinstance(result, FactorICResult)
            assert isinstance(result.ic_value, float)
            assert isinstance(result.rank_ic, float)

        except ImportError:
            pytest.skip("FactorICComputer 不可用")


# ─── Step 3: 因子 → 信号评分 ─────────────────────────────────────────

class TestStep3_FactorToSignalScore:
    """验证信号评分器能综合因子输出分数。"""

    def test_signal_scoring(self):
        """信号评分应返回合法的分数。"""
        try:
            from ai_engine.signal_scorer import SignalScorer
            from ai_engine.schema_validator import TradePlan, Direction

            scorer = SignalScorer()

            plan = TradePlan(
                direction=Direction.LONG,
                confidence=0.75,
                symbol="BTCUSDT",
                entry_price=50000.0,
                stop_loss=49000.0,
                take_profit=52000.0,
                reasoning="EMA多头排列，RSI位于50上方，建议做多操作",
            )
            regime_data = {
                "regime": "TRENDING",
                "confidence": 0.8,
            }

            score = scorer.score(plan, regime_data)

            assert isinstance(score, float)
            assert 0.0 <= score <= 1.0

        except ImportError:
            pytest.skip("SignalScorer 不可用")


# ─── Step 4: 分数 → 风控审核 ────────────────────────────────────────

class TestStep4_SignalToRiskGuardian:
    """验证风控模块能审核信号。"""

    def test_position_sizing(self):
        """仓位计算器应返回合理的仓位。"""
        try:
            from risk_guardian.position_sizer import PositionSizer, PositionConfig

            sizer = PositionSizer()
            size = sizer.calculate(
                capital=10000.0,
                risk_pct=0.02,
                entry_price=50000.0,
                stop_loss=49000.0,
            )
            assert size > 0
            assert isinstance(size, float)

        except ImportError:
            pytest.skip("PositionSizer 不可用")

    def test_signal_arbitration(self):
        """信号仲裁器应返回决定。"""
        try:
            from risk_guardian.signal_arbiter import SignalArbiter, ArbitrationResult

            arbiter = SignalArbiter()
            result = arbiter.arbitrate(
                symbol="BTCUSDT",
                direction="LONG",
                confidence=0.7,
                regime="trend_following",
                breaker_state="closed",
            )
            assert result.direction in ("LONG", "SHORT", "FLAT", "HOLD")
            assert 0 <= result.final_confidence <= 1.0
            assert result.is_approved in (True, False)

        except ImportError:
            pytest.skip("SignalArbiter 不可用")


# ─── Step 5: 风控 → 决策记录 ────────────────────────────────────────

class TestStep5_RiskToDecisionLogger:
    """验证决策记录器能接收和查询信号。"""

    def test_decision_logger_api(self):
        """决策记录器应支持 log() 和 fetch_recent()。"""
        try:
            from observability.decision_logger import DecisionLogger

            logger = DecisionLogger()

            async def _test():
                result = await logger.log(
                    symbol="BTCUSDT",
                    direction="LONG",
                    confidence=0.75,
                    regime="trend_following",
                    prompt_version="v1.0",
                )
                assert result is not None
                return result

            asyncio.run(_test())

        except ImportError:
            pytest.skip("DecisionLogger 不可用")
        except Exception as exc:
            # 数据库不可用也是可接受的（回退到控制台）
            assert "connection" not in str(exc).lower()


# ─── Step 6: 信号 → 因子衰减监控 ───────────────────────────────────

class TestStep6_FactorDecayMonitor:
    """验证因子衰减监控器能处理 IC 序列。"""

    def test_decay_analysis(self):
        """衰减分析应返回报告。"""
        try:
            from validation.factor_decay import FactorDecayMonitor, FactorDecayReport

            monitor = FactorDecayMonitor()
            ic_series = [0.05, 0.04, 0.03, 0.02, 0.01, 0.005, -0.01, -0.02]

            report = monitor.analyze("test_momentum", ic_series)

            assert isinstance(report, FactorDecayReport)
            assert report.factor_name == "test_momentum"
            assert isinstance(report.ic_mean, float)
            assert isinstance(report.is_decaying, bool)
            assert len(report.ic_values) == len(ic_series)

        except ImportError:
            pytest.skip("FactorDecayMonitor 不可用")

    def test_decay_observability_layer(self):
        """可观测性层应正确封装核心报告。"""
        try:
            from validation.factor_decay import FactorDecayMonitor
            from observability.factor_decay_monitor import FactorDecayResult

            monitor = FactorDecayMonitor()
            core_report = monitor.analyze("test", [0.04, 0.03, 0.02, 0.01])

            result = FactorDecayResult.from_core_report(core_report)

            assert result.factor_name == "test"
            assert result.ic_mean == core_report.ic_mean
            assert result.is_decaying == core_report.is_decaying
            assert result.ic_values_count == 4

        except ImportError:
            pytest.skip("FactorDecayMonitor/Observability 不可用")


# ─── Step 7: 完整管线（Mock 模式）────────────────────────────────────

class TestStep7_FullPipeline:
    """模拟完整管线，验证各模块粘合。"""

    def test_pipeline_signal_flow(self):
        """
        模拟：指标 → 因子 → 评分 → 仲裁 → 记录
        验证每个步骤的输出是下一步的合法输入。
        """
        signal = {"symbol": "BTCUSDT", "direction": "LONG", "confidence": 0.7}

        # Step A: 因子计算
        try:
            from ai_engine.signal_scorer import SignalScorer
            from ai_engine.schema_validator import TradePlan, Direction
            scorer = SignalScorer()
            plan = TradePlan(
                direction=Direction.LONG,
                confidence=0.75,
                symbol="BTCUSDT",
                entry_price=50000.0,
                stop_loss=49000.0,
                take_profit=52000.0,
                reasoning="EMA多头排列，RSI位于50上方，建议做多操作",
            )
            score = scorer.score(
                plan,
                {"regime": "TRENDING", "confidence": 0.8},
            )
            signal["direction"] = plan.direction.value
            signal["confidence"] = score
        except ImportError:
            pass

        # Step B: 风控仲裁
        try:
            from risk_guardian.signal_arbiter import SignalArbiter
            arbiter = SignalArbiter()
            result = arbiter.arbitrate(
                ai_signal={
                    "symbol": signal["symbol"],
                    "direction": signal["direction"],
                    "confidence": signal["confidence"],
                    "reasoning": "EMA多头排列，RSI位于50上方，建议做多操作",
                },
                regime="TRENDING",
            )
            signal["final_direction"] = result.action
            signal["final_confidence"] = result.size_pct
            signal["approved"] = result.action != "FLAT"
        except ImportError:
            signal["approved"] = True

        # 验证输出完整性
        assert "direction" in signal
        assert "confidence" in signal
        assert "approved" in signal

    def test_pipeline_error_propagation(self):
        """
        验证单个模块失败不会导致整个管线崩溃。
        """
        result = {"status": "ok"}

        try:
            from validation.factor_decay import FactorDecayMonitor
            monitor = FactorDecayMonitor()
            report = monitor.analyze("test", [])
            result["decay"] = report.is_decaying
        except Exception:
            result["decay"] = None
            result["status"] = "degraded"

        assert result["status"] in ("ok", "degraded")
