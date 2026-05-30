"""
测试: plan_generator.py

核心测试点:
- mock llm_client，测试 schema 校验失败时的降级路径
- 正常流程串联
- FLAT 信号处理
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_engine.plan_generator import PlanGenerator
from ai_engine.schema_validator import TradePlan, Direction


@pytest.fixture
def mock_llm_client():
    """Mock LLM 客户端，控制返回值和超时。"""
    client = AsyncMock()
    client.complete = AsyncMock()
    return client


@pytest.fixture
def mock_prompt_builder():
    """Mock PromptBuilder。"""
    builder = AsyncMock()
    builder.build = AsyncMock(return_value="test prompt")
    builder.template_name = "market_analysis.j2"
    return builder


@pytest.fixture
def mock_signal_scorer():
    """Mock SignalScorer。"""
    scorer = MagicMock()
    scorer.score = MagicMock(return_value=0.85)
    return scorer


@pytest.fixture
def mock_fallback_handler():
    """Mock FallbackHandler。"""
    handler = MagicMock()
    handler.handle = MagicMock(return_value=TradePlan(
        symbol="BTCUSDT",
        direction=Direction.FLAT,
        confidence=0.5,
        reasoning="[FALLBACK] Test fallback",
        regime="UNKNOWN",
        timeframe="1h",
    ))
    return handler


@pytest.fixture
def generator(mock_llm_client, mock_prompt_builder, mock_signal_scorer, mock_fallback_handler):
    """创建 PlanGenerator 实例（依赖全 mock）。"""
    return PlanGenerator(
        prompt_builder=mock_prompt_builder,
        llm_client=mock_llm_client,
        signal_scorer=mock_signal_scorer,
        fallback_handler=mock_fallback_handler,
    )


@pytest.fixture
def sample_indicators():
    """示例指标数据。"""
    return {
        "1h": {
            "EMA_9": 42100.0,
            "EMA_21": 42000.0,
            "EMA_55": 41800.0,
            "close": 42150.0,
            "SMA_20": 42000.0,
            "MACD_hist": 15.0,
            "RSI_14": 58.0,
            "ADX_14": 28.0,
            "VWAP": 42050.0,
        },
    }


@pytest.fixture
def sample_regime():
    """示例制度信号。"""
    return {
        "symbol": "BTCUSDT",
        "ts": 1700000000000,
        "regime": "TRENDING",
        "confidence": 0.85,
        "method": "rule_based",
    }


# ─── 正常流程测试 ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_plan_success(generator, mock_llm_client, sample_indicators, sample_regime):
    """正常流程：LLM 返回合法 JSON → 返回 TradePlan。"""
    mock_llm_client.complete.return_value = """
    ```json
    {
        "symbol": "BTCUSDT",
        "direction": "long",
        "confidence": 0.82,
        "entry_price": 42200.0,
        "stop_loss": 41500.0,
        "take_profit": 43500.0,
        "reasoning": "EMA多头排列，RSI未超买且位于50上方，趋势良好，建议做多操作",
        "regime": "TRENDING",
        "timeframe": "1h"
    }
    ```
    """
    plan = await generator.generate_plan(sample_indicators, sample_regime)
    assert plan is not None
    assert plan.direction == Direction.LONG
    assert plan.confidence == 0.82
    assert plan.symbol == "BTCUSDT"
    assert plan.score == 0.85  # mock 返回的值


@pytest.mark.asyncio
async def test_generate_plan_success_short(generator, mock_llm_client, sample_indicators, sample_regime):
    """正常流程：做空信号。"""
    mock_llm_client.complete.return_value = """
    ```json
    {
        "symbol": "BTCUSDT",
        "direction": "short",
        "confidence": 0.75,
        "entry_price": 41800.0,
        "stop_loss": 42500.0,
        "take_profit": 40500.0,
        "reasoning": "EMA死叉形成，RSI从超买区回落至50下方，建议做空操作",
        "regime": "TRENDING",
        "timeframe": "1h"
    }
    ```
    """
    plan = await generator.generate_plan(sample_indicators, sample_regime)
    assert plan is not None
    assert plan.direction == Direction.SHORT
    assert plan.confidence == 0.75


# ─── 降级路径测试 ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_failure_triggers_fallback(generator, mock_llm_client, mock_fallback_handler, sample_indicators, sample_regime):
    """LLM 返回 None → 触发 fallback 降级。"""
    mock_llm_client.complete.return_value = None

    plan = await generator.generate_plan(sample_indicators, sample_regime)

    # 验证 fallback 被调用
    mock_fallback_handler.handle.assert_called_once()
    # 返回 FLAT 信号
    assert plan is not None
    assert plan.direction == Direction.FLAT
    assert plan.reasoning is not None
    assert "FALLBACK" in plan.reasoning


@pytest.mark.asyncio
async def test_schema_validation_failure_triggers_fallback(generator, mock_llm_client, mock_fallback_handler, sample_indicators, sample_regime):
    """LLM 返回非法 JSON → Schema 校验失败 → 触发 fallback。"""
    mock_llm_client.complete.return_value = "这不是合法的 JSON 格式"

    plan = await generator.generate_plan(sample_indicators, sample_regime)

    mock_fallback_handler.handle.assert_called_once()
    assert plan is not None
    assert plan.direction == Direction.FLAT


@pytest.mark.asyncio
async def test_llm_timeout_triggers_fallback(generator, mock_llm_client, mock_fallback_handler, sample_indicators, sample_regime):
    """LLM 超时 → 触发 fallback。"""
    mock_llm_client.complete.return_value = None

    plan = await generator.generate_plan(sample_indicators, sample_regime)

    mock_fallback_handler.handle.assert_called_once()
    assert plan is not None
    assert plan.direction == Direction.FLAT


@pytest.mark.asyncio
async def test_prompt_builder_failure_triggers_fallback(generator, mock_prompt_builder, mock_fallback_handler, sample_indicators, sample_regime):
    """Prompt 构建失败 → 触发 fallback。"""
    mock_prompt_builder.build = AsyncMock(return_value=None)

    plan = await generator.generate_plan(sample_indicators, sample_regime)

    mock_fallback_handler.handle.assert_called_once()
    assert plan is not None
    assert plan.direction == Direction.FLAT


# ─── FLAT 信号处理 ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_flat_direction_handled(generator, mock_llm_client, sample_indicators, sample_regime):
    """LLM 返回 FLAT 方向 → 正常返回 TradePlan（FLAT 也是合法输出）。"""
    mock_llm_client.complete.return_value = """
    ```json
    {
        "symbol": "BTCUSDT",
        "direction": "flat",
        "confidence": 0.6,
        "entry_price": null,
        "stop_loss": null,
        "take_profit": null,
        "reasoning": "市场方向不明确，建议暂时观望等待，等待趋势明朗",
        "regime": "TRENDING",
        "timeframe": "1h"
    }
    ```
    """
    plan = await generator.generate_plan(sample_indicators, sample_regime)
    assert plan is not None
    assert plan.direction == Direction.FLAT


# ─── 信号输出格式测试 ───────────────────────────────────────

def test_to_signal_output(generator):
    """测试 to_signal 输出符合 ai_signal Stream 格式。"""
    plan = TradePlan(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        confidence=0.82,
        entry_price=42200.0,
        stop_loss=41500.0,
        take_profit=43500.0,
        reasoning="测试信号格式 — 这是一个用于验证信号格式输出的测试用例",
        regime="TRENDING",
        timeframe="1h",
    )
    plan.score = 0.85

    signal = generator.to_signal(plan, prompt_version="abc12345", is_fallback=False)

    assert signal["symbol"] == "BTCUSDT"
    assert signal["direction"] == "LONG"
    assert signal["confidence"] == 0.82
    assert signal["entry"] == 42200.0
    assert signal["sl"] == 41500.0
    assert signal["tp"] == 43500.0
    assert signal["score"] == 0.85
    assert signal["prompt_version"] == "abc12345"
    assert signal["regime"] == "TRENDING"
    assert signal["is_fallback"] is False
    assert "ts" in signal
    assert "reasoning" in signal


# ─── 状态管理测试 ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_last_valid_signal_cached(generator, mock_llm_client, sample_indicators, sample_regime):
    """非 FLAT 信号应缓存为 last_valid_signal。"""
    mock_llm_client.complete.return_value = """
    ```json
    {
        "symbol": "BTCUSDT",
        "direction": "long",
        "confidence": 0.82,
        "entry_price": 42200.0,
        "stop_loss": 41500.0,
        "take_profit": 43500.0,
        "reasoning": "EMA多头排列，RSI位于50上方，MACD柱状图正值，建议做多操作",
        "regime": "TRENDING",
        "timeframe": "1h"
    }
    """
    await generator.generate_plan(sample_indicators, sample_regime)
    assert generator.last_valid_signal is not None
    assert generator.last_valid_signal.direction == Direction.LONG


@pytest.mark.asyncio
async def test_reset_last_valid_signal(generator, mock_llm_client, sample_indicators, sample_regime):
    """重置 last_valid_signal。"""
    mock_llm_client.complete.return_value = """
    ```json
    {
        "symbol": "BTCUSDT",
        "direction": "long",
        "confidence": 0.82,
        "entry_price": 42200.0,
        "stop_loss": 41500.0,
        "take_profit": 43500.0,
        "reasoning": "EMA多头排列，RSI位于50上方，MACD柱状图正值，建议做多操作",
        "regime": "TRENDING",
        "timeframe": "1h"
    }
    """
    await generator.generate_plan(sample_indicators, sample_regime)
    assert generator.last_valid_signal is not None
    generator.reset_last_valid_signal()
    assert generator.last_valid_signal is None
