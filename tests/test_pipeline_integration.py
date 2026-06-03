"""
管道集成测试。

覆盖 4 层 processor 的完整数据流：
  raw_kline → indicators → regime_signal → ai_signal → trade_order

测试策略：
  - 不依赖 Redis（mock StreamProducer + StreamConsumer）
  - mock 外部依赖（LLM, Binance API, Freqtrade）
  - 验证每层输出的字段完整性和业务逻辑正确性

"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pandas as pd
import pytest

# ── 测试用 mock Stream ─────────────────────────────────────
# 用 AsyncMock 模拟 Redis Stream 的行为
# Stream 是一个 dict[str, list[dict]]，key = stream 名称


class MockStream:
    """
    内存中的 mock Redis Stream。
    用于替代 StreamProducer.publish() 和 StreamConsumer.subscribe()。
    """

    def __init__(self):
        self._streams: dict[str, list[dict[str, Any]]] = defaultdict(list)

    async def publish(self, stream: str, message: dict[str, Any]) -> None:
        self._streams[stream].append(message)

    def get_messages(self, stream: str) -> list[dict[str, Any]]:
        return list(self._streams.get(stream, []))

    def clear(self) -> None:
        self._streams.clear()

    def count(self, stream: str) -> int:
        return len(self._streams.get(stream, []))


# ── 共享 fixtures ──────────────────────────────────────────

@pytest.fixture
def mock_stream() -> MockStream:
    """内存 mock stream，替代 Redis。"""
    return MockStream()


@pytest.fixture
def sample_raw_kline() -> dict[str, Any]:
    """单条原始 K 线消息（来自 Binance WS）。"""
    return {
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "ts": 1700000000000,
        "open": "42000.00",
        "high": "42100.00",
        "low": "41900.00",
        "close": "42050.00",
        "volume": "123.45",
        "quote_volume": "5180000",
        "trades": 1234,
        "is_closed": True,
        "taker_buy_volume": "60.00",
        "taker_buy_quote": "2500000",
        "event_time": 1700000000001,
    }


def _build_kline_cache(count: int) -> list[dict[str, Any]]:
    """
    生成 count 条模拟 K 线，填充 indicators processor 的预热缓存。
    价格在前 200 条平缓上升（EMA 多头排列），之后震荡。
    """
    cache = []
    for i in range(count):
        base = 42000.0 + (i * 0.5 if i < 200 else 0)
        noise = (i % 10) * 0.1
        cache.append({
            "symbol": "BTCUSDT",
            "timeframe": "1m",
            "ts": 1700000000000 + (i * 60_000),
            "open": str(base + noise),
            "high": str(base + 50 + noise),
            "low": str(base - 50 + noise),
            "close": str(base + 10 + noise),
            "volume": "100.00",
            "quote_volume": str(100.0 * base),
            "trades": 1000 + i,
            "is_closed": True,
        })
    return cache


@pytest.fixture
def warmup_kline_messages() -> list[dict[str, Any]]:
    """用于指标预热的标准 K 线批量。"""
    return _build_kline_cache(210)


@pytest.fixture
def sample_indicators_output() -> dict[str, Any]:
    """模拟 indicators processor 的输出（含指标）。"""
    return {
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "ts": 1700000000000,
        "close": 42050.0,
        "volume": 123.45,
        "cached_kline_count": 210,
        "indicators": {
            "EMA_9": 42045.0,
            "EMA_21": 42030.0,
            "EMA_55": 42010.0,
            "EMA_200": 41800.0,
            "close": 42050.0,
            "SMA_20": 42020.0,
            "MACD": 15.0,
            "MACDh": 5.0,
            "MACDs": 10.0,
            "RSI_14": 58.0,
            "ADX_14": 28.0,
            "VWAP": 42025.0,
        },
    }


@pytest.fixture
def mock_llm_response() -> str:
    """模拟 LLM 返回的有效交易计划 JSON。"""
    return """
    ```json
    {
        "symbol": "BTCUSDT",
        "direction": "long",
        "confidence": 0.82,
        "entry_price": 42200.0,
        "stop_loss": 41500.0,
        "take_profit": 43500.0,
        "reasoning": "EMA多头排列，ADX>25确认趋势，建议做多",
        "regime": "TRENDING",
        "timeframe": "1h"
    }
    ```
    """


# ═══════════════════════════════════════════════════════════════
#  第一层：indicators/processor.py
# ═══════════════════════════════════════════════════════════════


class TestIndicatorsProcessor:
    """指标处理器集成测试。"""

    @pytest.mark.asyncio
    async def test_cache_warmup_skips_early_messages(self, sample_raw_kline):
        """预热阶段应跳过消息，返回 None。"""
        from indicators.processor import process_raw_kline, clear_cache
        clear_cache()

        # 前 199 条应返回 None（预热不足）
        for i in range(199):
            msg = dict(sample_raw_kline, ts=1700000000000 + i * 60_000)
            result = await process_raw_kline(msg)
            assert result is None, f"第 {i+1} 条预热不应产出结果"

    @pytest.mark.asyncio
    async def test_cache_warmup_produces_result(self, warmup_kline_messages):
        """预热完成后应返回有效的 indicators 消息。"""
        from indicators.processor import process_raw_kline, clear_cache
        clear_cache()

        result = None
        for msg in warmup_kline_messages:
            result = await process_raw_kline(msg)

        assert result is not None, "预热后应产出结果"
        assert result["symbol"] == "BTCUSDT"
        assert result["timeframe"] == "1m"
        assert "indicators" in result
        assert len(result["indicators"]) > 0, "应包含指标"

    @pytest.mark.asyncio
    async def test_indicators_contains_expected_fields(self, warmup_kline_messages):
        """输出的 indicators 应包含 EMA, MACD, RSI, ADX, VWAP 等核心指标。"""
        from indicators.processor import process_raw_kline, clear_cache
        clear_cache()

        result = None
        for msg in warmup_kline_messages:
            result = await process_raw_kline(msg)

        assert result is not None
        ind = result["indicators"]

        # 趋势指标
        assert any("EMA_" in k for k in ind), f"应包含 EMA 指标, keys={list(ind.keys())[:10]}"
        assert any("MACD" in k for k in ind), "应包含 MACD 指标"

        # 动量指标
        assert "RSI_14" in ind, "应包含 RSI"

        # 波动率指标
        assert any("ATR" in k for k in ind), "应包含 ATR"

        # 成交量指标
        assert any("VWAP" in k or "OBV" in k for k in ind), "应包含成交量指标"

    @pytest.mark.asyncio
    async def test_cache_overflow_trimmed(self, warmup_kline_messages):
        """缓存超过 300 条时应自动修剪。"""
        from indicators.processor import process_raw_kline, clear_cache
        clear_cache()

        # 发送 310 条（超过缓存上限 300）
        for i in range(310):
            msg = dict(warmup_kline_messages[0], ts=1700000000000 + i * 60_000)
            await process_raw_kline(msg)

        # 再发一条应仍能正常产出（不因缓存溢出崩溃）
        msg = dict(warmup_kline_messages[0], ts=1700000000000 + 310 * 60_000)
        result = await process_raw_kline(msg)
        assert result is not None

    @pytest.mark.asyncio
    async def test_multiple_symbols_independent(self):
        """不同交易对的缓存应独立。"""
        from indicators.processor import process_raw_kline, clear_cache
        clear_cache()

        btc_msgs = _build_kline_cache(200)
        eth_msgs = _build_kline_cache(200)

        # 交替发送 BTC 和 ETH 的 K 线
        for i in range(200):
            btc = dict(btc_msgs[i], symbol="BTCUSDT")
            eth = dict(eth_msgs[i], symbol="ETHUSDT")
            await process_raw_kline(btc)
            await process_raw_kline(eth)

        # 第 201 条 BTC 应产出结果
        btc_final = dict(btc_msgs[0], symbol="BTCUSDT", ts=1700000000000 + 200 * 60_000)
        r1 = await process_raw_kline(btc_final)

        # ETH 还需要 1 条才能预热完成
        eth_final = dict(eth_msgs[0], symbol="ETHUSDT", ts=1700000000000 + 200 * 60_000)
        r2 = await process_raw_kline(eth_final)

        assert r1 is not None, "BTC 应完成预热"
        assert r1["symbol"] == "BTCUSDT"
        assert r2 is not None, "ETH 应完成预热"
        assert r2["symbol"] == "ETHUSDT"


# ═══════════════════════════════════════════════════════════════
#  第二层：regime/processor.py
# ═══════════════════════════════════════════════════════════════


class TestRegimeProcessor:
    """制度识别处理器集成测试。"""

    @pytest.mark.asyncio
    async def test_empty_indicators_skipped(self):
        """空指标消息应跳过。"""
        from regime.processor import process_indicators

        msg = {"symbol": "BTCUSDT", "indicators": {}}
        result = await process_indicators(msg)
        assert result is None

    @pytest.mark.asyncio
    async def test_trending_regime_detected(self, sample_indicators_output):
        """ADX > 25 且 BB 宽度适中应识别为 TRENDING。"""
        from regime.processor import process_indicators

        # TRENDING 指标：ADX=28
        msg = dict(
            sample_indicators_output,
            indicators={
                "ADX_14": 28.0,
                "close": 42000.0,
                "EMA_9": 42100.0,
                "BBU_20_2": 42500.0,
                "BBL_20_2": 41500.0,
                "BBM_20_2": 42000.0,
                "volume": 100.0,
            },
        )
        result = await process_indicators(msg)
        assert result is not None
        assert result["regime"] == "TRENDING"
        assert result["confidence"] > 0.5
        assert result["adx"] == 28.0

    @pytest.mark.asyncio
    async def test_ranging_regime_detected(self, sample_indicators_output):
        """ADX < 20 且 BB 宽度窄应识别为 RANGING。"""
        from regime.processor import process_indicators

        msg = dict(
            sample_indicators_output,
            indicators={
                "ADX_14": 15.0,
                "close": 42000.0,
                "volume": 100.0,
            },
        )
        result = await process_indicators(msg)
        assert result is not None
        assert result["regime"] == "RANGING"

        # 注意：当 BB 宽度数据不足时，BB 宽度会默认为 0.0，
        # 而 RANGING 判断需要 bb_width < 0.02 且 ADX < 20
        # 所以 bb_width=0.0 会通过
        assert result["confidence"] >= 0.7

    @pytest.mark.asyncio
    async def test_high_volatility_detected(self, sample_indicators_output):
        """BB 宽度极大（> 0.08）应识别为 HIGH_VOLATILITY。"""
        from regime.processor import process_indicators

        msg = dict(
            sample_indicators_output,
            indicators={
                "ADX_14": 30.0,
                "close": 42000.0,
                "BBU_20_2": 45000.0,
                "BBL_20_2": 39000.0,
                "BBM_20_2": 42000.0,
                "volume": 100.0,
            },
        )
        result = await process_indicators(msg)
        assert result is not None
        assert result["regime"] == "HIGH_VOLATILITY"
        assert result["confidence"] == 0.85

    @pytest.mark.asyncio
    async def test_bb_width_via_standardized_field(self, sample_indicators_output):
        """应支持 BBW_20_2 标准化字段。"""
        from regime.processor import process_indicators

        msg = dict(
            sample_indicators_output,
            indicators={
                "ADX_14": 35.0,
                "BBW_20_2": 0.015,
                "close": 42000.0,
                "volume": 100.0,
            },
        )
        result = await process_indicators(msg)
        assert result is not None

        # BBW=0.015 (窄) 且 ADX=35 (高)，BB 宽度优先
        # HIGH_VOLATILITY 要求 bb_width > 0.08，此处 0.015 < 0.08
        # TRENDING 要求 ADX > 25，bb_width 无上限 → TRENDING
        assert result["regime"] == "TRENDING"

    @pytest.mark.asyncio
    async def test_output_contains_required_fields(self, sample_indicators_output):
        """regime_signal 输出应包含所有必需字段。"""
        from regime.processor import process_indicators

        msg = dict(
            sample_indicators_output,
            indicators={"ADX_14": 28.0, "close": 42000.0, "volume": 100.0},
        )
        result = await process_indicators(msg)
        assert result is not None

        required = {"symbol", "ts", "regime", "confidence", "adx", "bb_width", "close", "method"}
        for field in required:
            assert field in result, f"缺少必需字段: {field}"
        assert result["method"] == "rule_based"


# ═══════════════════════════════════════════════════════════════
#  第三层：ai_engine/processor.py
# ═══════════════════════════════════════════════════════════════


class TestAiEngineProcessor:
    """AI 引擎处理器集成测试。"""

    @pytest.mark.asyncio
    async def test_empty_indicators_skipped(self):
        """无指标数据时应跳过。"""
        from ai_engine.processor import process_regime_signal

        msg = {"symbol": "BTCUSDT", "regime": "TRENDING", "confidence": 0.85}
        result = await process_regime_signal(msg)
        assert result is None

    @pytest.mark.asyncio
    async def test_low_confidence_signal_emitted(self):
        """低置信度也应返回信号（不抛异常）。"""
        from ai_engine.processor import process_regime_signal

        msg = {
            "symbol": "BTCUSDT",
            "ts": 1700000000000,
            "regime": "TRENDING",
            "confidence": 0.6,
            "close": 42000.0,
        }
        # PlanGenerator 需要 LLM，这会被 mock 或触发 fallback
        # 使用 patch 替换 PlanGenerator 为 mock
        with patch("ai_engine.plan_generator.PlanGenerator") as MockGen:
            mock_instance = AsyncMock()
            mock_instance.generate_plan = AsyncMock(return_value=None)
            mock_instance.to_signal = MagicMock()
            MockGen.return_value = mock_instance

            result = await process_regime_signal(msg)

        # 当 generate_plan 返回 None 时，应生成 FLAT 信号
        assert result is not None
        assert result.get("direction") == "FLAT"

    @pytest.mark.asyncio
    async def test_full_ai_plan_generated(self, mock_llm_response):
        """完整流程：regime_signal → LLM → TradePlan → ai_signal。"""
        from ai_engine.processor import process_regime_signal

        # 模拟完整的 PlanGenerator 路径
        with patch.multiple(
            "ai_engine.plan_generator",
            PromptBuilder=MagicMock(),
            LLMClient=MagicMock(),
            SignalScorer=MagicMock(),
            StrategyAdapter=MagicMock(),
            FallbackHandler=MagicMock(),
        ):
            # patch PlanGenerator.generate_plan
            with patch("ai_engine.processor._get_generator") as mock_get_gen:
                mock_gen = AsyncMock()
                mock_plan = MagicMock()
                mock_plan.direction = MagicMock()
                mock_plan.direction.value = "LONG"
                mock_plan.confidence = 0.82
                mock_plan.score = 0.85
                mock_plan.symbol = "BTCUSDT"
                mock_plan.regime = "TRENDING"
                mock_gen.generate_plan = AsyncMock(return_value=mock_plan)
                mock_gen.to_signal = MagicMock(return_value={
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "confidence": 0.82,
                    "score": 0.85,
                    "entry": 42200.0,
                    "sl": 41500.0,
                    "tp": 43500.0,
                    "reasoning": "EMA多头排列",
                    "regime": "TRENDING",
                    "ts": 1700000000000,
                    "prompt_version": "test-v1",
                    "is_fallback": False,
                })
                mock_get_gen.return_value = mock_gen

                # 也需要 mock prompt_versioner
                with patch("ai_engine.processor._get_versioner") as mock_get_ver:
                    mock_ver = MagicMock()
                    mock_ver.get_version = MagicMock(return_value="test-v1")
                    mock_get_ver.return_value = mock_ver

                    msg = {
                        "symbol": "BTCUSDT",
                        "ts": 1700000000000,
                        "regime": "TRENDING",
                        "confidence": 0.85,
                        "close": 42100.0,
                        "adx": 28.0,
                        "bb_width": 0.042,
                    }
                    result = await process_regime_signal(msg)

        assert result is not None
        assert result.get("direction") == "LONG"
        assert result.get("confidence") == 0.82

    @pytest.mark.asyncio
    async def test_regime_signal_fields_propagated(self):
        """regime_signal 的字段应正确传播到 ai_signal。"""
        from ai_engine.processor import process_regime_signal

        with patch("ai_engine.processor._get_generator") as mock_get_gen:
            mock_gen = AsyncMock()
            mock_plan = MagicMock()
            mock_plan.direction = MagicMock()
            mock_plan.direction.value = "FLAT"
            mock_plan.confidence = 0.0
            mock_plan.score = 0.0
            mock_plan.symbol = "BTCUSDT"
            mock_plan.regime = "TRENDING"
            mock_gen.generate_plan = AsyncMock(return_value=mock_plan)
            mock_gen.to_signal = MagicMock(return_value={
                "direction": "FLAT",
                "confidence": 0.0,
                "score": 0.0,
                "symbol": "BTCUSDT",
                "ts": 1700000000000,
                "is_fallback": False,
            })
            mock_get_gen.return_value = mock_gen

            with patch("ai_engine.processor._get_versioner") as mock_get_ver:
                mock_ver = MagicMock()
                mock_ver.get_version = MagicMock(return_value="v1")
                mock_get_ver.return_value = mock_ver

                msg = {
                    "symbol": "BTCUSDT",
                    "ts": 1700000000000,
                    "regime": "TRENDING",
                    "confidence": 0.85,
                    "close": 42100.0,
                }
                result = await process_regime_signal(msg)

        assert result is not None
        assert result.get("symbol") == "BTCUSDT"
        assert result.get("regime") == "TRENDING"


# ═══════════════════════════════════════════════════════════════
#  第四层：risk_guardian/processor.py
# ═══════════════════════════════════════════════════════════════


class TestRiskGuardianProcessor:
    """风控处理器集成测试。"""

    @pytest.mark.asyncio
    async def test_long_signal_accepted(self):
        """LONG 高置信度信号应通过风控审核。"""
        from risk_guardian.processor import process_ai_signal

        with patch("risk_guardian.signal_arbiter.SignalArbiter") as MockArbiter:
            mock_arbiter = MagicMock()
            mock_order = MagicMock()
            mock_order.action = "LONG"
            mock_order.size_pct = 0.08
            mock_order.entry = 42200.0
            mock_order.sl = 41500.0
            mock_order.tp = 43500.0
            mock_order.source = "ai_signal"
            mock_order.breaker_state = "CLOSED"
            mock_order.audit_id = "test-uuid"
            mock_order.reasoning = "AI 信号"
            mock_order.to_stream_message = MagicMock(return_value={
                "symbol": "BTCUSDT",
                "ts": 1700000000000,
                "action": "LONG",
                "size_pct": 0.08,
                "entry": 42200.0,
                "sl": 41500.0,
                "tp": 43500.0,
                "source": "ai_signal",
                "breaker_state": "CLOSED",
                "audit_id": "test-uuid",
                "reasoning": "AI 信号",
            })
            mock_arbiter.arbitrate = MagicMock(return_value=mock_order)
            MockArbiter.return_value = mock_arbiter

            with patch("risk_guardian.processor._get_arbiter", return_value=mock_arbiter):
                with patch("risk_guardian.processor._get_breaker") as mock_get_breaker:
                    mock_breaker = MagicMock()
                    mock_get_breaker.return_value = mock_breaker

                    msg = {
                        "symbol": "BTCUSDT",
                        "ts": 1700000000000,
                        "direction": "LONG",
                        "confidence": 0.85,
                        "score": 0.85,
                        "regime": "TRENDING",
                        "reason": "EMA 多头排列",
                    }
                    result = await process_ai_signal(msg)

        assert result is not None
        assert result.get("action") == "LONG"
        assert result.get("size_pct") == 0.08
        assert result.get("audit_id") == "test-uuid"

    @pytest.mark.asyncio
    async def test_low_confidence_signal_filtered(self):
        """低置信度信号应被风控过滤（仍返回 trade_order，但可能为 FLAT）。"""
        from risk_guardian.processor import process_ai_signal

        with patch("risk_guardian.processor._get_arbiter") as mock_get_arbiter:
            mock_arbiter = MagicMock()
            mock_order = MagicMock()
            mock_order.action = "FLAT"
            mock_order.size_pct = 0.0
            mock_order.reasoning = "置信度不足"
            mock_order.to_stream_message = MagicMock(return_value={
                "symbol": "BTCUSDT",
                "ts": 1700000000000,
                "action": "FLAT",
                "size_pct": 0.0,
                "source": "ai_signal",
                "breaker_state": "CLOSED",
                "audit_id": "test-uuid",
                "reasoning": "置信度不足",
            })
            mock_arbiter.arbitrate = MagicMock(return_value=mock_order)
            mock_get_arbiter.return_value = mock_arbiter

            msg = {
                "symbol": "BTCUSDT",
                "ts": 1700000000000,
                "direction": "LONG",
                "confidence": 0.3,
                "score": 0.3,
                "regime": "TRENDING",
            }
            result = await process_ai_signal(msg)

        assert result is not None
        assert result.get("action") == "FLAT"

    @pytest.mark.asyncio
    async def test_arbitrate_exception_handled(self):
        """arbitrate 抛出异常时应返回 FLAT。"""
        from risk_guardian.processor import process_ai_signal

        with patch("risk_guardian.processor._get_arbiter") as mock_get_arbiter:
            mock_arbiter = MagicMock()
            mock_arbiter.arbitrate = MagicMock(side_effect=ConnectionError("Redis 断开"))
            mock_get_arbiter.return_value = mock_arbiter

            msg = {
                "symbol": "BTCUSDT",
                "ts": 1700000000000,
                "direction": "LONG",
                "confidence": 0.85,
                "score": 0.85,
                "regime": "TRENDING",
            }
            result = await process_ai_signal(msg)

        assert result is not None
        assert result.get("direction") == "FLAT"
        assert "风控异常" in result.get("reason", "")


# ═══════════════════════════════════════════════════════════════
#  跨层集成测试
# ═══════════════════════════════════════════════════════════════


class TestCrossLayerPipeline:
    """跨层管道集成测试 — 模拟完整数据流。"""

    @pytest.mark.asyncio
    async def test_indicators_to_regime_pipeline(self, warmup_kline_messages):
        """indicators processor → regime processor 的完整数据流。"""
        from indicators.processor import process_raw_kline, clear_cache
        from regime.processor import process_indicators
        clear_cache()

        # 1. 预热指标缓存
        indicators_msg = None
        for msg in warmup_kline_messages:
            indicators_msg = await process_raw_kline(msg)

        assert indicators_msg is not None, "指标预热应产出结果"

        # 2. 将 indicators 消息传递给 regime processor
        regime_msg = await process_indicators(indicators_msg)

        assert regime_msg is not None, "制度识别应产出结果"
        assert regime_msg["symbol"] == indicators_msg["symbol"]
        assert "regime" in regime_msg
        assert "confidence" in regime_msg
        assert "adx" in regime_msg

    @pytest.mark.asyncio
    async def test_regime_to_ai_signal_pipeline(self, sample_indicators_output):
        """regime processor → ai_engine processor 的数据流。"""
        from regime.processor import process_indicators
        from ai_engine.processor import process_regime_signal

        # 1. 从 indicators 生成 regime 信号
        regime_msg = await process_indicators(sample_indicators_output)
        assert regime_msg is not None
        assert regime_msg["regime"] is not None

        # 2. 传递给 AI 引擎（mock LLM）
        with patch("ai_engine.processor._get_generator") as mock_get_gen:
            mock_gen = AsyncMock()
            mock_plan = MagicMock()
            mock_plan.direction = MagicMock()
            mock_plan.direction.value = "FLAT"
            mock_plan.confidence = 0.0
            mock_plan.score = 0.0
            mock_plan.symbol = "BTCUSDT"
            mock_plan.regime = regime_msg["regime"]
            mock_gen.generate_plan = AsyncMock(return_value=mock_plan)
            mock_gen.to_signal = MagicMock(return_value={
                "direction": "FLAT",
                "confidence": 0.0,
                "score": 0.0,
                "symbol": "BTCUSDT",
                "ts": 1700000000000,
                "is_fallback": False,
            })
            mock_get_gen.return_value = mock_gen

            with patch("ai_engine.processor._get_versioner") as mock_get_ver:
                mock_ver = MagicMock()
                mock_ver.get_version = MagicMock(return_value="v1")
                mock_get_ver.return_value = mock_ver

                ai_msg = await process_regime_signal(regime_msg)

        assert ai_msg is not None
        assert ai_msg.get("direction") is not None

    @pytest.mark.asyncio
    async def test_ai_to_risk_pipeline(self):
        """ai_engine → risk_guardian 的数据流。"""
        from ai_engine.processor import process_regime_signal
        from risk_guardian.processor import process_ai_signal

        # 1. 生成 ai_signal
        with patch("ai_engine.processor._get_generator") as mock_get_gen:
            mock_gen = AsyncMock()
            mock_plan = MagicMock()
            mock_plan.direction = MagicMock()
            mock_plan.direction.value = "LONG"
            mock_plan.confidence = 0.85
            mock_plan.score = 0.85
            mock_plan.symbol = "BTCUSDT"
            mock_plan.regime = "TRENDING"
            mock_gen.generate_plan = AsyncMock(return_value=mock_plan)
            mock_gen.to_signal = MagicMock(return_value={
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "confidence": 0.85,
                "score": 0.85,
                "entry": 42200.0,
                "sl": 41500.0,
                "tp": 43500.0,
                "reasoning": "EMA 多头排列",
                "regime": "TRENDING",
                "ts": 1700000000000,
                "prompt_version": "v1",
                "is_fallback": False,
            })
            mock_get_gen.return_value = mock_gen

            with patch("ai_engine.processor._get_versioner") as mock_get_ver:
                mock_ver = MagicMock()
                mock_ver.get_version = MagicMock(return_value="v1")
                mock_get_ver.return_value = mock_ver

                ai_msg = await process_regime_signal({
                    "symbol": "BTCUSDT",
                    "ts": 1700000000000,
                    "regime": "TRENDING",
                    "confidence": 0.85,
                    "close": 42100.0,
                })

        assert ai_msg is not None
        assert ai_msg.get("direction") == "LONG"

        # 2. 传递给风控
        with patch("risk_guardian.processor._get_arbiter") as mock_get_arbiter:
            mock_arbiter = MagicMock()
            mock_order = MagicMock()
            mock_order.action = "LONG"
            mock_order.size_pct = 0.08
            mock_order.entry = 42200.0
            mock_order.sl = 41500.0
            mock_order.tp = 43500.0
            mock_order.source = "ai_signal"
            mock_order.breaker_state = "CLOSED"
            mock_order.audit_id = "audit-001"
            mock_order.reasoning = "AI 信号通过风控"
            mock_order.to_stream_message = MagicMock(return_value={
                "symbol": "BTCUSDT",
                "ts": 1700000000000,
                "action": "LONG",
                "size_pct": 0.08,
                "entry": 42200.0,
                "sl": 41500.0,
                "tp": 43500.0,
                "source": "ai_signal",
                "breaker_state": "CLOSED",
                "audit_id": "audit-001",
                "reasoning": "AI 信号通过风控",
            })
            mock_arbiter.arbitrate = MagicMock(return_value=mock_order)
            mock_get_arbiter.return_value = mock_arbiter

            risk_msg = await process_ai_signal(ai_msg)

        assert risk_msg is not None
        assert risk_msg.get("action") == "LONG"
        assert risk_msg.get("size_pct") == 0.08
        assert risk_msg.get("audit_id") == "audit-001"
        assert "is_fallback" in risk_msg
        assert risk_msg.get("regime") == "TRENDING"

    def test_full_trade_order_format(self):
        """trade_order 最终输出格式应与 AiSignalStrategy 期望的格式一致。"""
        # 模拟完整的 trade_order 消息
        order = {
            "symbol": "BTCUSDT",
            "ts": 1700000000000,
            "action": "LONG",
            "size_pct": 0.08,
            "entry": 42200.0,
            "sl": 41500.0,
            "tp": 43500.0,
            "source": "ai_signal",
            "breaker_state": "CLOSED",
            "audit_id": "audit-001",
            "reasoning": "AI 信号通过风控",
            "score": 0.85,
            "is_fallback": False,
            "regime": "TRENDING",
        }

        # AiSignalStrategy.populate_entry_trend 期望的字段
        assert "action" in order
        assert order["action"] in ("LONG", "SHORT", "FLAT", "FORCE_EXIT")
        assert "size_pct" in order
        assert "entry" in order
        assert "sl" in order
        assert "tp" in order
        assert "audit_id" in order

        # AiSignalStrategy._fetch_sync 解析的字段
        payload_json = json.dumps(order)
        parsed = json.loads(payload_json)
        assert parsed["action"] == "LONG"
        assert parsed["size_pct"] == 0.08


# ═══════════════════════════════════════════════════════════════
#  consumer 消费者集成测试
# ═══════════════════════════════════════════════════════════════


class TestConsumerIntegration:
    """messaging/consumer.py 集成测试。"""

    @pytest.mark.asyncio
    async def test_dynamic_processor_loading(self):
        """动态模块加载应能正确加载 4 个处理器。"""
        from messaging.consumer import _STREAM_PROCESSOR_MAP

        for stream, module_path in _STREAM_PROCESSOR_MAP.items():
            module_name, func_name = module_path.split(":")
            import importlib

            try:
                module = importlib.import_module(module_name)
                func = getattr(module, func_name)
                assert asyncio.iscoroutinefunction(func), f"{module_path} 不是 async 函数"
            except (ImportError, AttributeError) as exc:
                pytest.fail(f"处理器加载失败 {module_path}: {exc}")

    def test_output_stream_mapping_complete(self):
        """每个输入 Stream 都应有对应的输出 Stream。"""
        from messaging.consumer import _STREAM_PROCESSOR_MAP, _OUTPUT_STREAM_MAP

        for stream in _STREAM_PROCESSOR_MAP:
            assert stream in _OUTPUT_STREAM_MAP, f"{stream} 缺少输出 Stream 映射"

    def test_all_processors_have_valid_functions(self):
        """所有 _STREAM_PROCESSOR_MAP 的函数名应与实际文件中的函数名一致。"""
        from messaging.consumer import _STREAM_PROCESSOR_MAP

        for stream, module_path in _STREAM_PROCESSOR_MAP.items():
            module_name, func_name = module_path.split(":")
            import importlib

            try:
                module = importlib.import_module(module_name)
                assert hasattr(module, func_name), f"{module_name} 中未找到 {func_name}"
            except ImportError as exc:
                pytest.fail(f"模块加载失败 {module_name}: {exc}")


# ═══════════════════════════════════════════════════════════════
#  orchestrator 编排器集成测试
# ═══════════════════════════════════════════════════════════════


class TestOrchestrator:
    """app/orchestrator.py 集成测试。"""

    def test_worker_registry_has_all_workers(self):
        """worker 注册表应包含全部 5 个 worker。"""
        from app.orchestrator import _WORKERS

        expected = {"data", "indicators", "regime", "ai_engine", "risk"}
        assert set(_WORKERS.keys()) == expected

    def test_worker_funcs_exist(self):
        """每个 worker 的协程函数应存在于模块中。"""
        from app.orchestrator import _WORKER_FUNCS, _get_worker_func

        for name in _WORKER_FUNCS:
            coro = _get_worker_func(name)
            assert asyncio.iscoroutinefunction(coro), f"{name} 的 worker 不是协程函数"

    def test_parse_args_default(self):
        """不传参数时应默认启动所有 worker。"""
        from app.orchestrator import parse_args

        args = parse_args([])
        assert args.worker is None

    def test_parse_args_with_worker(self):
        """--worker data 应正确解析。"""
        from app.orchestrator import parse_args

        args = parse_args(["--worker", "data"])
        assert args.worker == "data"

    def test_parse_args_invalid_worker(self):
        """无效 worker 名应报错。"""
        from app.orchestrator import parse_args

        with pytest.raises(SystemExit):
            parse_args(["--worker", "invalid"])

    def test_worker_descriptions(self):
        """所有 worker 应有中文描述。"""
        from app.orchestrator import _WORKERS

        for name, (name_zh, _) in _WORKERS.items():
            assert name_zh, f"{name} 缺少中文描述"
