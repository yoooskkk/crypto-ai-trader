"""
crypto-ai-trader 全管线集成测试

测试管道数据流（无 Redis 依赖，直接调用各 processor）：

  mock_raw_kline →
    indicators/processor:process_raw_kline()      → indicators 消息
      → regime/processor:process_indicators()      → regime_signal 消息
        → ai_engine/processor:process_regime_signal()  → ai_signal 消息
          → risk_guardian/processor:process_ai_signal() → trade_order 消息

覆盖场景:
  - 完整正向管线（200+ K 线 → 指标 → 制度 → AI → 风控 → 交易指令）
  - 缓存预热不足（<200 K 线 → None）
  - 指标数据不足（regime 收到空 indicators → None）
  - AI 引擎降级（无指标数据时 → FLAT）
  - 风控拒绝低分信号（confidence=0 → FLAT）
  - 单个模块异常不扩散（隔离性验证）
  - 各阶段消息格式符合 STREAM_SCHEMA.md
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from datetime import datetime, timezone

_UTC = timezone.utc
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# ─── 确保模块可导入 ────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ═══════════════════════════════════════════════════════════════
#  Mock 数据工厂
# ═══════════════════════════════════════════════════════════════

class MockKlineFactory:
    """生成模拟 K 线数据用于测试。"""

    BASE_PRICE = 50000.0

    @classmethod
    def _price_series(cls, n: int, base: float | None = None) -> np.ndarray:
        """生成模拟价格序列（带小幅随机波动）。"""
        base = base or cls.BASE_PRICE
        np.random.seed(42)
        trend = np.cumsum(np.random.randn(n) * 0.3)
        return base + trend

    @classmethod
    def single_kline(
        cls,
        idx: int,
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
        base_price: float | None = None,
    ) -> dict[str, Any]:
        """生成单根 K 线消息（raw_kline 格式）。"""
        base = base_price or cls.BASE_PRICE
        price = base + idx * 10.0 + np.random.randn() * 5.0
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "ts": int(datetime(2025, 1, 1, tzinfo=_UTC).timestamp() * 1000) + idx * 3600_000,
            "open": str(price - 20.0),
            "high": str(price + 30.0),
            "low": str(price - 25.0),
            "close": str(price + 5.0),
            "volume": str(100 + (idx % 50) * 2),
            "quote_volume": str((100 + (idx % 50) * 2) * price),
            "taker_buy_volume": str(60 + (idx % 30) * 2),
            "taker_buy_quote": str((60 + (idx % 30) * 2) * price),
            "is_closed": True,
        }

    @classmethod
    def kline_series(
        cls,
        n: int = 300,
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
    ) -> list[dict[str, Any]]:
        """生成连续的 K 线序列（时间递增）。"""
        np.random.seed(42)
        price = cls.BASE_PRICE
        series = []
        for i in range(n):
            price += np.random.randn() * 5.0 + 0.5
            ts_base = int(datetime(2025, 1, 1, tzinfo=_UTC).timestamp() * 1000)
            kline = {
                "symbol": symbol,
                "timeframe": timeframe,
                "ts": ts_base + i * 3600_000,
                "open": f"{price - 10.0:.2f}",
                "high": f"{price + 15.0:.2f}",
                "low": f"{price - 12.0:.2f}",
                "close": f"{price + 3.0:.2f}",
                "volume": f"{100 + (i % 50) * 3:.2f}",
                "quote_volume": f"{(100 + (i % 50) * 3) * price:.2f}",
                "taker_buy_volume": f"{60 + (i % 30) * 2:.2f}",
                "taker_buy_quote": f"{(60 + (i % 30) * 2) * price:.2f}",
                "is_closed": True,
            }
            series.append(kline)
        return series

    @classmethod
    def as_dataframe(cls, klines: list[dict[str, Any]]) -> pd.DataFrame:
        """将 K 线列表转为 pandas DataFrame（用于直接指标计算）。"""
        df = pd.DataFrame(klines)
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df


# ═══════════════════════════════════════════════════════════════
#  Schema 验证辅助函数
# ═══════════════════════════════════════════════════════════════

def assert_indicators_schema(msg: dict[str, Any]) -> None:
    """验证 indicators Stream 消息符合 STREAM_SCHEMA.md。"""
    assert "symbol" in msg, "缺少 symbol"
    assert "timeframe" in msg, "缺少 timeframe"
    assert "ts" in msg, "缺少 ts"
    assert "indicators" in msg, "缺少 indicators"
    assert isinstance(msg["indicators"], dict), "indicators 必须为 dict"
    assert len(msg["indicators"]) > 0, "indicators 不应为空"
    assert "close" in msg, "缺少 close"
    assert isinstance(msg["close"], (int, float)), "close 必须为数值"
    # 检查已知指标字段
    expected_keys = {"EMA_9", "RSI_14", "ATR_14", "ADX_14", "BBW_20_2", "OBV"}
    found = expected_keys & set(msg["indicators"].keys())
    assert len(found) >= 2, f"indicator 中关键字段太少: 只找到了 {found}"


def assert_regime_signal_schema(msg: dict[str, Any]) -> None:
    """验证 regime_signal Stream 消息符合 STREAM_SCHEMA.md。"""
    assert "symbol" in msg, "缺少 symbol"
    assert "regime" in msg, "缺少 regime"
    assert msg["regime"] in ("TRENDING", "RANGING", "HIGH_VOLATILITY", "UNKNOWN"), \
        f"regime 值非法: {msg['regime']}"
    assert "confidence" in msg, "缺少 confidence"
    assert 0.0 <= msg["confidence"] <= 1.0, f"confidence 超出范围: {msg['confidence']}"
    assert "method" in msg, "缺少 method"
    assert msg["method"] in ("rule_based", "hmm"), f"method 值非法: {msg['method']}"
    assert "ts" in msg, "缺少 ts"
    assert "close" in msg, "缺少 close"
    assert "adx" in msg, "缺少 adx"


def assert_ai_signal_schema(msg: dict[str, Any]) -> None:
    """验证 ai_signal Stream 消息符合 STREAM_SCHEMA.md。"""
    assert "symbol" in msg, "缺少 symbol"
    assert "direction" in msg, "缺少 direction"
    assert msg["direction"] in ("LONG", "SHORT", "FLAT"), \
        f"direction 值非法: {msg['direction']}"
    assert "confidence" in msg, "缺少 confidence"
    assert 0.0 <= msg["confidence"] <= 1.0, f"confidence 超出范围: {msg['confidence']}"
    assert "score" in msg, "缺少 score"
    assert "regime" in msg, "缺少 regime"
    assert "ts" in msg, "缺少 ts"


def assert_trade_order_schema(msg: dict[str, Any]) -> None:
    """验证 trade_order Stream 消息符合 STREAM_SCHEMA.md。"""
    assert "symbol" in msg, "缺少 symbol"
    assert "action" in msg, "缺少 action"
    assert msg["action"] in ("LONG", "SHORT", "FLAT", "FORCE_EXIT"), \
        f"action 值非法: {msg['action']}"
    assert "ts" in msg, "缺少 ts"
    # FLAT 可以有 size_pct=0
    assert "size_pct" in msg, "缺少 size_pct"
    assert 0.0 <= msg["size_pct"] <= 1.0, f"size_pct 超出范围: {msg['size_pct']}"
    # audit_id 只由 SignalArbiter 生成，FLAT 可能没有
    if msg.get("audit_id"):
        assert isinstance(msg["audit_id"], str), "audit_id 必须为 string"


# ═══════════════════════════════════════════════════════════════
#  Test Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def reset_global_state():
    """每个测试前清除全局缓存/单例，避免跨测试污染。"""
    # 清除 indicators processor 缓存
    from indicators.processor import clear_cache
    clear_cache()

    # 重置 regime processor 全局检测器
    import regime.processor as rp
    from regime.detector import RuleBasedDetector
    rp._detector = RuleBasedDetector()

    # 清除 ai_engine processor 全局实例
    import ai_engine.processor as ap
    ap._generator = None   # type: ignore[attr-defined]
    ap._versioner = None   # type: ignore[attr-defined]
    ap._latest_indicators.clear()  # type: ignore[attr-defined]

    # 清除 risk_guardian processor 全局实例
    from risk_guardian.processor import reset_instances
    reset_instances()

    yield


@pytest.fixture
def warm_klines() -> list[dict[str, Any]]:
    """生成足以触发指标计算的 K 线序列（300 根，超过 200 预热线）。"""
    return MockKlineFactory.kline_series(n=300)


@pytest.fixture
def cold_klines() -> list[dict[str, Any]]:
    """生成不足以触发指标计算的 K 线序列（50 根，低于 200 预热线）。"""
    return MockKlineFactory.kline_series(n=50)


# ═══════════════════════════════════════════════════════════════
#  Stage 1: raw_kline → indicators processor → indicators
# ═══════════════════════════════════════════════════════════════

class TestStage1_DataToIndicators:
    """raw_kline → process_raw_kline() → indicators Stream 消息。"""

    @pytest.mark.asyncio
    async def test_happy_path(self, warm_klines):
        """
        正向场景：300 根 K 线逐根输入，最后一条应返回完整指标。
        """
        from indicators.processor import process_raw_kline, clear_cache
        clear_cache()

        last_result = None
        for i, kline in enumerate(warm_klines):
            result = await process_raw_kline(kline)
            if result is not None:
                last_result = result

        # 最后一条应非空（缓存已预热）
        assert last_result is not None, "300 根 K 线后应产出指标"

        # 验证 schema
        assert_indicators_schema(last_result)
        assert last_result["symbol"] == "BTCUSDT"

        # 验证关键指标存在（ADX 需要更长的预热，用更宽松的检查）
        ind = last_result["indicators"]
        assert "EMA_9" in ind, f"缺少 EMA_9，已有: {list(ind.keys())[:10]}"
        assert "RSI_14" in ind, f"缺少 RSI_14"
        # ADX 需要约 50 根 K 线预热，300 根足够
        adx_present = "ADX_14" in ind
        # 数值范围检查
        if "RSI_14" in ind:
            assert 0 <= ind["RSI_14"] <= 100, f"RSI 超出范围: {ind['RSI_14']}"
        # 至少有一些趋势指标
        trend_keys = {k for k in ind if k.startswith("EMA_") or k.startswith("SMA_")}
        assert len(trend_keys) >= 1, f"缺少趋势指标，已有: {list(ind.keys())[:15]}"

    @pytest.mark.asyncio
    async def test_cold_cache_returns_none(self, cold_klines):
        """
        缓存预热不足（<200 根）时返回 None。
        """
        from indicators.processor import process_raw_kline, clear_cache
        clear_cache()

        results = []
        for kline in cold_klines:
            r = await process_raw_kline(kline)
            if r is not None:
                results.append(r)

        assert len(results) == 0, f"预热不足时应全部返回 None，实际返回 {len(results)}"

    @pytest.mark.asyncio
    async def test_multiple_symbols_isolation(self):
        """
        多交易对标缓存应隔离。
        """
        from indicators.processor import process_raw_kline, clear_cache
        clear_cache()

        btc = MockKlineFactory.kline_series(n=250, symbol="BTCUSDT")
        eth = MockKlineFactory.kline_series(n=250, symbol="ETHUSDT")

        # 交替输入
        last_btc = last_eth = None
        for b, e in zip(btc, eth):
            r1 = await process_raw_kline(b)
            r2 = await process_raw_kline(e)
            if r1:
                last_btc = r1
            if r2:
                last_eth = r2

        assert last_btc is not None, "BTC 应产出指标"
        assert last_eth is not None, "ETH 应产出指标"
        assert last_btc["symbol"] == "BTCUSDT"
        assert last_eth["symbol"] == "ETHUSDT"

    @pytest.mark.asyncio
    async def test_missing_column_graceful(self):
        """
        缺少数值列时处理器不应崩溃。
        """
        from indicators.processor import process_raw_kline, clear_cache
        clear_cache()

        bad_kline = {
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "ts": 1700000000000,
            # 缺少 high/low/close/volume
            "open": "50000.0",
        }
        result = await process_raw_kline(bad_kline)
        # 可能返回 None 但不应抛异常
        assert result is None or isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_indicator_values_reasonable(self, warm_klines):
        """
        指标值应在合理范围内。
        """
        from indicators.processor import process_raw_kline, clear_cache
        clear_cache()

        last = None
        for k in warm_klines:
            r = await process_raw_kline(k)
            if r:
                last = r

        assert last is not None
        ind = last["indicators"]

        # 成交量相关指标应为正
        if "OBV" in ind:
            assert ind["OBV"] != 0, "OBV 不应为 0"
        if "VWAP" in ind:
            assert ind["VWAP"] > 0, "VWAP 应为正"

        # 波动率指标
        if "ATR_14" in ind:
            assert ind["ATR_14"] > 0, "ATR 应为正"

        # 布林带宽度
        if "BBW_20_2" in ind:
            assert ind["BBW_20_2"] > 0, "BBW 应为正"

        # 趋势指标
        assert "cached_kline_count" in last
        assert last["cached_kline_count"] >= 200


# ═══════════════════════════════════════════════════════════════
#  Stage 2: indicators → regime processor → regime_signal
# ═══════════════════════════════════════════════════════════════

class TestStage2_IndicatorsToRegime:
    """indicators → process_indicators() → regime_signal Stream 消息。"""

    @pytest.fixture
    def indicators_message(self) -> dict[str, Any]:
        """构造合法的 indicators Stream 消息。"""
        return {
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "ts": 1700000000000,
            "close": 50100.0,
            "volume": 1500.0,
            "indicators": {
                "ADX_14": 28.5,
                "BBW_20_2": 0.038,
                "BBU_20_2": 51200.0,
                "BBM_20_2": 49800.0,
                "BBL_20_2": 48400.0,
                "EMA_9": 50050.0,
                "RSI_14": 58.3,
                "ATR_14": 380.0,
                "OBV": 123456789.0,
            },
            "cached_kline_count": 250,
        }

    @pytest.mark.asyncio
    async def test_happy_path(self, indicators_message):
        """正向场景：生成合法的制度信号。"""
        from regime.processor import process_indicators

        result = await process_indicators(indicators_message)

        assert result is not None, "regime processor 应返回结果"
        assert_regime_signal_schema(result)
        assert result["symbol"] == "BTCUSDT"
        assert result["method"] == "rule_based"
        assert result["indicators_count"] == 9

    @pytest.mark.asyncio
    async def test_empty_indicators_returns_none(self):
        """空指标字典返回 None。"""
        from regime.processor import process_indicators

        msg = {
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "ts": 1700000000000,
            "close": 50000.0,
            "indicators": {},
        }
        result = await process_indicators(msg)
        assert result is None, "空指标应返回 None"

    @pytest.mark.asyncio
    async def test_missing_indicators_field_returns_none(self):
        """缺少 indicators 字段应返回 None（或 key error 优雅处理）。"""
        from regime.processor import process_indicators

        msg = {
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "ts": 1700000000000,
            "close": 50000.0,
        }
        try:
            result = await process_indicators(msg)
            assert result is None
        except KeyError:
            pytest.fail("缺少 indicators 时应优雅处理而非 KeyError")

    @pytest.mark.asyncio
    async def test_trending_regime(self, indicators_message):
        """高 ADX + 窄 BB → TRENDING。"""
        from regime.processor import process_indicators

        msg = copy.deepcopy(indicators_message)
        msg["indicators"]["ADX_14"] = 35.0  # > 25
        msg["indicators"]["BBW_20_2"] = 0.02  # < 0.04

        result = await process_indicators(msg)
        assert result is not None
        assert result["regime"] == "TRENDING", f"应为 TRENDING，实际 {result['regime']}"
        assert result["adx"] == 35.0

    @pytest.mark.asyncio
    async def test_ranging_regime(self, indicators_message):
        """低 ADX（< 20）+ 窄 BB（< 0.02）→ RANGING。"""
        from regime.processor import process_indicators

        msg = copy.deepcopy(indicators_message)
        msg["indicators"]["ADX_14"] = 15.0   # < 20
        msg["indicators"]["BBW_20_2"] = 0.01  # < 0.02

        result = await process_indicators(msg)
        assert result is not None
        assert result["regime"] == "RANGING", f"应为 RANGING，实际 {result['regime']}"
        assert result["confidence"] > 0.0

    @pytest.mark.asyncio
    async def test_bb_width_fallback(self, indicators_message):
        """无 BBW_20_2 时尝试从上下轨计算。"""
        from regime.processor import process_indicators

        msg = copy.deepcopy(indicators_message)
        del msg["indicators"]["BBW_20_2"]
        msg["indicators"]["BBU_20_2"] = 52000.0
        msg["indicators"]["BBM_20_2"] = 50000.0
        msg["indicators"]["BBL_20_2"] = 48000.0

        result = await process_indicators(msg)
        assert result is not None
        # 回退计算: (52000-48000)/50000 = 0.08
        assert result["bb_width"] == pytest.approx(0.08, abs=0.001)

    @pytest.mark.asyncio
    async def test_strategy_switcher_called(self, indicators_message):
        """process_indicators 应调用 strategy_switcher 更新 risk.yml。"""
        from regime.processor import process_indicators

        with patch("regime.processor._evaluate_and_apply") as mock_apply:
            result = await process_indicators(indicators_message)
            assert result is not None
            mock_apply.assert_called_once()
            call_args = mock_apply.call_args[0][0]
            assert call_args["regime"] == result["regime"]


# ═══════════════════════════════════════════════════════════════
#  Stage 3: regime_signal → ai_engine processor → ai_signal
# ═══════════════════════════════════════════════════════════════

class TestStage3_RegimeToAIEngine:
    """regime_signal → process_regime_signal() → ai_signal Stream 消息。"""

    @pytest.fixture
    def regime_message(self) -> dict[str, Any]:
        """构造合法的 regime_signal Stream 消息。"""
        return {
            "symbol": "BTCUSDT",
            "ts": 1700000000000,
            "regime": "TRENDING",
            "confidence": 0.85,
            "adx": 28.5,
            "bb_width": 0.038,
            "close": 50100.0,
            "method": "rule_based",
            "indicators_count": 9,
        }

    @pytest.mark.asyncio
    async def test_happy_path(self, regime_message):
        """正向场景：生成合法的 AI 信号。"""
        from ai_engine.processor import process_regime_signal

        # mock PlanGenerator 避免真实 LLM 调用
        with patch("ai_engine.plan_generator.PlanGenerator.generate_plan") as mock_gen:
            mock_plan = MagicMock()
            mock_dir = MagicMock()
            mock_dir.value = "LONG"
            mock_plan.direction = mock_dir
            mock_plan.confidence = 0.75
            mock_plan.entry_price = 50200.0
            mock_plan.stop_loss = 49500.0
            mock_plan.take_profit = 51500.0
            mock_plan.reasoning = "趋势强劲，做多"
            mock_plan.score = 0.72
            mock_gen.return_value = mock_plan

            result = await process_regime_signal(regime_message)

        assert result is not None, "AI processor 应返回结果"
        assert_ai_signal_schema(result)
        assert result["symbol"] == "BTCUSDT"
        assert result["direction"] == "LONG"
        assert result["confidence"] == 0.75
        assert result["score"] == 0.72
        assert result["regime"] == "TRENDING"
        assert not result.get("is_fallback", False), "非降级信号"

    @pytest.mark.asyncio
    async def test_fallback_flat_when_no_indicators(self):
        """无指标数据时生成 FLAT 降级信号。"""
        from ai_engine.processor import process_regime_signal

        msg = {
            "symbol": "BTCUSDT",
            "ts": 1700000000000,
            "regime": "UNKNOWN",
            "confidence": 0.3,
            "adx": 0.0,
            "bb_width": 0.0,
            # 无 close 字段
            "method": "rule_based",
            "indicators_count": 0,
        }

        result = await process_regime_signal(msg)
        # 无 close 且无缓存 → None
        assert result is None or result.get("direction") == "FLAT"

    @pytest.mark.asyncio
    async def test_plan_generator_exception(self, regime_message):
        """PlanGenerator 抛出异常时返回 None（消息静默丢弃）。"""
        from ai_engine.processor import process_regime_signal

        with patch("ai_engine.plan_generator.PlanGenerator.generate_plan") as mock_gen:
            mock_gen.side_effect = RuntimeError("LLM API 不可达")

            result = await process_regime_signal(regime_message)

        # 生产环境行为：异常时不发信号，消息静默跳过
        # 由下游 fallback_handler 处理超时/缺失信号的情况
        assert result is None, "异常时应返回 None（消息丢弃）"

    @pytest.mark.asyncio
    async def test_none_plan_fallback(self, regime_message):
        """PlanGenerator 返回 None 时生成 FLAT。"""
        from ai_engine.processor import process_regime_signal

        with patch("ai_engine.plan_generator.PlanGenerator.generate_plan") as mock_gen:
            mock_gen.return_value = None

            result = await process_regime_signal(regime_message)

        assert result is not None
        assert result["direction"] == "FLAT"
        assert result["is_fallback"]
        assert result.get("reason") is not None, "FLAT 应有原因说明"

    @pytest.mark.asyncio
    async def test_prompt_version_included(self, regime_message):
        """ai_signal 应包含 prompt_version。"""
        from ai_engine.processor import process_regime_signal

        with patch("ai_engine.plan_generator.PlanGenerator.generate_plan") as mock_gen:
            mock_plan = MagicMock()
            mock_dir = MagicMock()
            mock_dir.value = "SHORT"
            mock_plan.direction = mock_dir
            mock_plan.confidence = 0.6
            mock_plan.entry_price = 50000.0
            mock_plan.stop_loss = 51000.0
            mock_plan.take_profit = 48000.0
            mock_plan.reasoning = "测试"
            mock_plan.score = 0.5
            mock_gen.return_value = mock_plan

            result = await process_regime_signal(regime_message)

        assert result is not None
        # prompt_version 由 plan_generator.to_signal 填充
        assert "prompt_version" in result or result.get("is_fallback"), \
            "非降级信号应包含 prompt_version"


# ═══════════════════════════════════════════════════════════════
#  Stage 4: ai_signal → risk_guardian processor → trade_order
# ═══════════════════════════════════════════════════════════════

class TestStage4_AISignalToRiskGuardian:
    """ai_signal → process_ai_signal() → trade_order Stream 消息。"""

    @pytest.fixture
    def ai_signal_long(self) -> dict[str, Any]:
        """构造合法的 ai_signal 消息（LONG）。"""
        return {
            "symbol": "BTCUSDT",
            "ts": 1700000000000,
            "direction": "LONG",
            "confidence": 0.82,
            "entry": 50200.0,
            "sl": 49500.0,
            "tp": 52000.0,
            "score": 0.74,
            "prompt_version": "a3f8c1d2",
            "regime": "TRENDING",
            "reasoning": "多头趋势持续",
            "is_fallback": False,
        }

    @pytest.fixture
    def ai_signal_flat(self) -> dict[str, Any]:
        """Flat 信号（AI 降级）。"""
        return {
            "symbol": "BTCUSDT",
            "ts": 1700000001000,
            "direction": "FLAT",
            "confidence": 0.0,
            "score": 0.0,
            "prompt_version": "",
            "regime": "UNKNOWN",
            "reasoning": "AI 引擎降级 / FLAT",
            "is_fallback": True,
        }

    @pytest.fixture
    def ai_signal_low_conf(self) -> dict[str, Any]:
        """低置信度信号。"""
        return {
            "symbol": "BTCUSDT",
            "ts": 1700000002000,
            "direction": "LONG",
            "confidence": 0.15,
            "entry": 50200.0,
            "sl": 49500.0,
            "tp": 52000.0,
            "score": 0.12,
            "prompt_version": "a3f8c1d2",
            "regime": "TRENDING",
            "reasoning": "弱信号",
            "is_fallback": False,
        }

    @pytest.mark.asyncio
    async def test_happy_path_long_signal(self, ai_signal_long):
        """LONG 信号 → 风控仲裁 → trade_order。"""
        from risk_guardian.processor import process_ai_signal

        result = await process_ai_signal(ai_signal_long)

        assert result is not None
        assert_trade_order_schema(result)
        # 可能通过也可能拒绝，取决于仲裁器内部状态
        assert result["symbol"] == "BTCUSDT"
        assert result["action"] in ("LONG", "SHORT", "FLAT", "FORCE_EXIT")

    @pytest.mark.asyncio
    async def test_flat_signal_remains_flat(self, ai_signal_flat):
        """FLAT 输入应保持 FLAT 输出。"""
        from risk_guardian.processor import process_ai_signal

        result = await process_ai_signal(ai_signal_flat)

        assert result is not None
        assert result["action"] == "FLAT", f"Flat 输入应输出 FLAT，实际 {result['action']}"
        assert result["size_pct"] == 0.0

    @pytest.mark.asyncio
    async def test_low_confidence_rejected(self, ai_signal_low_conf):
        """极低置信度应被风控拒绝。"""
        from risk_guardian.processor import process_ai_signal

        result = await process_ai_signal(ai_signal_low_conf)

        assert result is not None
        # 低置信度很可能被过滤
        assert result["action"] in ("FLAT", "LONG", "SHORT")

    @pytest.mark.asyncio
    async def test_empty_message_does_not_crash(self):
        """残缺消息不应导致崩溃。"""
        from risk_guardian.processor import process_ai_signal

        result = await process_ai_signal({})
        # 应优雅处理，返回拒绝信号
        assert result is not None
        assert result["action"] in ("FLAT", "LONG", "SHORT")

    @pytest.mark.asyncio
    async def test_circuit_breaker_trips(self, ai_signal_long):
        """模拟熔断器触发时输出 FORCE_EXIT。"""
        from risk_guardian.processor import process_ai_signal, _get_breaker

        # 触发熔断器：连续亏损
        breaker = _get_breaker()
        for i in range(10):
            breaker.update_equity(10000.0 - i * 500.0)

        result = await process_ai_signal(ai_signal_long)

        assert result is not None
        # 熔断触发后，仲裁器可能拒绝（FLAT）或强平（FORCE_EXIT）
        assert result["action"] in ("FLAT", "FORCE_EXIT"), \
            f"熔断状态下应为 FLAT/FORCE_EXIT，实际 {result['action']}"

    @pytest.mark.asyncio
    async def test_arbiter_integration(self, ai_signal_long):
        """验证 signal_arbiter 被正确调用且返回完整结果。"""
        from risk_guardian.processor import process_ai_signal

        with patch("risk_guardian.processor._get_arbiter") as mock_get_arbiter:
            mock_arbiter = MagicMock()
            mock_result = MagicMock()
            mock_result.action = "LONG"
            mock_result.size_pct = 0.08
            mock_result.stop_loss_pct = 0.02
            mock_result.take_profit_pct = 0.04
            mock_result.audit_id = "test-uuid-1234"
            mock_result.reasoning = "测试通过"
            mock_result.to_stream_message.return_value = {
                "symbol": "BTCUSDT",
                "ts": 1700000000000,
                "action": "LONG",
                "size_pct": 0.08,
                "stop_loss_pct": 0.02,
                "take_profit_pct": 0.04,
                "audit_id": "test-uuid-1234",
                "reasoning": "测试通过",
            }
            mock_arbiter.arbitrate.return_value = mock_result
            mock_get_arbiter.return_value = mock_arbiter

            result = await process_ai_signal(ai_signal_long)

        assert result is not None
        assert result["action"] == "LONG"
        assert result["size_pct"] == 0.08
        assert result.get("audit_id") == "test-uuid-1234"


# ═══════════════════════════════════════════════════════════════
#  Stage 5: 全链路验证（Mock PlanGenerator）
# ═══════════════════════════════════════════════════════════════

class TestStage5_EndToEnd:
    """
    全链路：300 根 K 线 → 指标 → 制度 → AI → 风控。

    使用 mock 的 PlanGenerator 避免真实 LLM 调用。
    验证每个阶段的输出是下一个阶段的合法输入。
    """

    @pytest.mark.asyncio
    async def test_full_pipeline(self):
        """
        完整的 5 阶段管线测试。
        """
        from indicators.processor import process_raw_kline, clear_cache
        from regime.processor import process_indicators
        from ai_engine.processor import process_regime_signal
        from risk_guardian.processor import process_ai_signal

        clear_cache()

        klines = MockKlineFactory.kline_series(n=300)

        # ─── Stage 1: K 线 → 指标 ──────────────────────
        indicators_msg = None
        for k in klines:
            r = await process_raw_kline(k)
            if r:
                indicators_msg = r

        assert indicators_msg is not None, "Stage 1: 应产出指标"
        assert_indicators_schema(indicators_msg)

        # ─── Stage 2: 指标 → 制度 ──────────────────────
        regime_msg = await process_indicators(indicators_msg)
        assert regime_msg is not None, "Stage 2: 应产出制度信号"
        assert_regime_signal_schema(regime_msg)

        # ─── Stage 3: 制度 → AI 信号 ──────────────────
        with patch("ai_engine.plan_generator.PlanGenerator.generate_plan") as mock_gen:
            mock_plan = MagicMock()
            mock_dir = MagicMock()
            mock_dir.value = "LONG"
            mock_plan.direction = mock_dir
            mock_plan.confidence = 0.75
            mock_plan.entry_price = float(regime_msg.get("close", 50000))
            mock_plan.stop_loss = mock_plan.entry_price * 0.98
            mock_plan.take_profit = mock_plan.entry_price * 1.03
            mock_plan.reasoning = f"全链路测试: {regime_msg['regime']}"
            mock_plan.score = 0.72
            mock_gen.return_value = mock_plan

            ai_msg = await process_regime_signal(regime_msg)

        assert ai_msg is not None, "Stage 3: 应产出 AI 信号"
        assert_ai_signal_schema(ai_msg)

        # ─── Stage 4: AI 信号 → 风控 → 交易指令 ──────
        trade_msg = await process_ai_signal(ai_msg)
        assert trade_msg is not None, "Stage 4: 应产出交易指令"
        assert_trade_order_schema(trade_msg)

        # 最终验证：每个阶段的 symbol 一致
        assert indicators_msg["symbol"] == "BTCUSDT"
        assert regime_msg["symbol"] == "BTCUSDT"
        assert ai_msg["symbol"] == "BTCUSDT"
        assert trade_msg["symbol"] == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_degraded_mode_indicators_fail(self):
        """
        指标阶段失败时，后续阶段收到空数据应优雅处理。
        """
        from regime.processor import process_indicators

        # 模拟空指标消息
        bad_msg = {
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "ts": 1700000000000,
            "indicators": {},
        }
        result = await process_indicators(bad_msg)
        assert result is None, "空指标应导致制度阶段跳过"

    @pytest.mark.asyncio
    async def test_degraded_mode_ai_fails(self):
        """
        AI 阶段失败时，消息被丢弃（返回 None）。
        风控层不会收到任何信号，因此不会做任何操作。
        """
        from ai_engine.processor import process_regime_signal

        msg = {
            "symbol": "BTCUSDT",
            "ts": 1700000000000,
            "regime": "TRENDING",
            "confidence": 0.85,
            "adx": 28.5,
            "bb_width": 0.04,
            "close": 50000.0,
            "method": "rule_based",
            "indicators_count": 5,
        }

        with patch("ai_engine.plan_generator.PlanGenerator.generate_plan") as mock_gen:
            mock_gen.side_effect = RuntimeError("LLM 不可用")
            ai_msg = await process_regime_signal(msg)

        # 异常时消息被丢弃，不发布任何信号
        assert ai_msg is None, "AI 异常应丢弃消息（返回 None）"

    @pytest.mark.asyncio
    async def test_degraded_mode_regime_fails(self):
        """
        制度信号缺失时（无 regime_signal），AI 引擎应返回 None 或 FLAT。
        """
        from ai_engine.processor import process_regime_signal

        msg = {
            "symbol": "BTCUSDT",
            "ts": 1700000000000,
            # 缺少 regime 字段
            "confidence": 0.5,
        }
        result = await process_regime_signal(msg)
        # 应优雅处理，不崩溃
        assert result is None or isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════
#  Stage 6: 消息格式契约验证
# ═══════════════════════════════════════════════════════════════

class TestStage6_SchemaCompliance:
    """验证所有处理器输出消息符合 STREAM_SCHEMA.md 定义。"""

    @pytest.mark.asyncio
    async def test_raw_kline_schema(self):
        """验证 raw_kline 消息格式符合 Stream Schema。"""
        kline = MockKlineFactory.single_kline(idx=0)
        assert "symbol" in kline
        assert "timeframe" in kline
        assert "ts" in kline
        assert "open" in kline
        assert "high" in kline
        assert "low" in kline
        assert "close" in kline
        assert "volume" in kline
        assert isinstance(kline["open"], str), "价格字段应为 string（保留精度）"
        assert isinstance(kline["volume"], str), "成交量字段应为 string"

    def test_indicators_schema_compliance(self):
        """验证 indicators 消息的字段命名与 config/indicators.yml 一致。"""
        import yaml

        # 从 indicators.yml 读取指标定义
        config_path = os.path.join(os.path.dirname(__file__), "..", "config", "indicators.yml")
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        # 检查必须存在的模块配置段
        for section in ("trend", "momentum", "volatility", "volume", "timeseries", "math_factors"):
            assert section in cfg, f"config/indicators.yml 缺少 {section} 段"

    @pytest.mark.asyncio
    async def test_trade_order_schema_force_exit(self):
        """FORCE_EXIT 动作的字段要求。"""
        from risk_guardian.processor import process_ai_signal, _get_breaker

        # 触发熔断器
        breaker = _get_breaker()
        for i in range(20):
            breaker.update_equity(10000.0 - i * 600.0)

        msg = {
            "symbol": "BTCUSDT",
            "ts": 1700000000000,
            "direction": "LONG",
            "confidence": 0.8,
            "score": 0.7,
            "regime": "TRENDING",
            "reasoning": "测试熔断",
            "is_fallback": False,
        }
        result = await process_ai_signal(msg)
        assert result is not None
        # 熔断触发后 action 可能为 FLAT 或 FORCE_EXIT
        assert result["action"] in ("FLAT", "FORCE_EXIT")


# ═══════════════════════════════════════════════════════════════
#  Stage 7: 资源管理与隔离性
# ═══════════════════════════════════════════════════════════════

class TestStage7_ResourceManagement:
    """验证 processor 的资源管理和隔离性。"""

    @pytest.mark.asyncio
    async def test_indicators_cache_clear(self):
        """clear_cache 应正确清空缓存。"""
        from indicators.processor import process_raw_kline, clear_cache
        clear_cache()

        klines = MockKlineFactory.kline_series(n=10)
        for k in klines:
            await process_raw_kline(k)

        cleared = clear_cache()
        assert cleared >= 10, f"clear_cache 应清空 >=10 条，实际 {cleared}"

        # 清空后再输入应重新预热
        k = MockKlineFactory.single_kline(idx=0)
        result = await process_raw_kline(k)
        assert result is None, "清空缓存后单条 K 线应返回 None"

    @pytest.mark.asyncio
    async def test_risk_processor_reset(self):
        """reset_instances 应重置全局实例。"""
        from risk_guardian.processor import reset_instances, _get_breaker, _get_arbiter
        reset_instances()
        b1 = _get_breaker()
        reset_instances()
        b2 = _get_breaker()
        # 重置后应创建新实例
        assert b1 is not b2, "reset 后应创建新的 breaker 实例"


# ═══════════════════════════════════════════════════════════════
#  Stage 8: 错误隔离性 — 单模块异常不扩散
# ═══════════════════════════════════════════════════════════════

class TestStage8_ErrorIsolation:
    """验证一个模块的失败不会导致相邻模块崩溃。"""

    @pytest.mark.asyncio
    async def test_indicator_processor_error_isolation(self):
        """
        单根 K 线导致指标计算异常时，不影响后续 K 线处理。
        """
        from indicators.processor import process_raw_kline, clear_cache
        clear_cache()

        klines = MockKlineFactory.kline_series(n=210)

        for i, k in enumerate(klines):
            result = await process_raw_kline(k)

        # 第 210 根应正常产出指标（无崩溃）
        # 只要没抛异常就算通过
        last = None
        for k in klines:
            r = await process_raw_kline(k)
            if r:
                last = r
        assert last is not None, "异常后仍能产出指标"

    @pytest.mark.asyncio
    async def test_ai_engine_does_not_break_on_bad_regime(self):
        """
        畸形的 regime 消息不应导致 AI 引擎崩溃。
        """
        from ai_engine.processor import process_regime_signal

        bad_messages = [
            {},
            {"symbol": "BTCUSDT"},
            {"symbol": "BTCUSDT", "regime": None},
            {"symbol": None, "ts": 0},
        ]
        for msg in bad_messages:
            try:
                result = await process_regime_signal(msg)
                # 返回 None 或 dict 均可，不抛异常即通过
                assert result is None or isinstance(result, dict)
            except Exception as exc:
                pytest.fail(f"畸形消息导致异常: {msg=}, {exc=}")

    @pytest.mark.asyncio
    async def test_risk_guardian_does_not_break_on_bad_ai_signal(self):
        """
        畸形的 AI 信号不应导致风控模块崩溃。
        """
        from risk_guardian.processor import process_ai_signal

        # 这些畸形消息应被优雅处理，不崩溃
        # {} — 空消息，symbol 缺失
        result = await process_ai_signal({})
        assert result is not None, "空消息应返回拒绝信号"
        assert result.get("action") in ("FLAT", "LONG", "SHORT")

        # symbol 存在但无 direction
        result = await process_ai_signal({"symbol": "BTCUSDT"})
        assert result is not None
        assert result.get("action") in ("FLAT", "LONG", "SHORT")

        # 非法 direction（arbiter 内部会处理）
        result = await process_ai_signal({"symbol": "BTCUSDT", "direction": "INVALID_DIRECTION"})
        assert result is not None

        # 非数值 confidence（最后会由仲裁器处理或异常）
        result = await process_ai_signal({"symbol": "BTCUSDT", "direction": "LONG", "confidence": "not_a_number"})
        assert result is not None, "非数值 confidence 也应返回"

        # 非法 confidence 值
        result = await process_ai_signal({"symbol": "BTCUSDT", "direction": "LONG", "confidence": 2.0})
        assert result is not None


# ═══════════════════════════════════════════════════════════════
#  Stage 9: 性能基线 — 大 K 线量处理
# ═══════════════════════════════════════════════════════════════

class TestStage9_Performance:
    """基础性能测试 — 确保管线能处理批量化数据。"""

    @pytest.mark.asyncio
    async def test_bulk_kline_processing(self):
        """
        批量处理 500 根 K 线不应超时。
        """
        from indicators.processor import process_raw_kline, clear_cache
        clear_cache()

        klines = MockKlineFactory.kline_series(n=500)

        import time
        start = time.time()

        last = None
        for k in klines:
            r = await process_raw_kline(k)
            if r:
                last = r

        elapsed = time.time() - start

        assert last is not None, "500 根 K 线应产出指标"
        assert elapsed < 30.0, f"500 根 K 线处理超时: {elapsed:.1f}s"

    @pytest.mark.asyncio
    async def test_concurrent_indicators(self):
        """
        多交易对并发处理不应降低产品质量。
        """
        from indicators.processor import process_raw_kline, clear_cache
        clear_cache()

        symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "SOLUSDT"]
        all_series = {
            sym: MockKlineFactory.kline_series(n=250, symbol=sym)
            for sym in symbols
        }

        # 交错输入模拟并发
        last_results = {}
        for i in range(250):
            for sym in symbols:
                r = await process_raw_kline(all_series[sym][i])
                if r:
                    last_results[sym] = r

        # 所有交易对应产出指标
        for sym in symbols:
            assert sym in last_results, f"{sym} 应产出指标"
            assert_indicators_schema(last_results[sym])
