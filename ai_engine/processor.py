"""
模块名称: processor.py
所属层级: AI 引擎层 (AI Engine)
输入来源: regime_signal Stream
输出去向: ai_signal Stream（经 PlanGenerator 生成的交易信号）
关键依赖: plan_generator (PlanGenerator), prompt_builder

regime_signal → AI 交易信号管道。

消费 regime_signal Stream，调用 PlanGenerator 生成交易计划，
将结果发布到 ai_signal Stream。

铁律 #5：LLM 输出必须经 schema_validator.py 校验后才能流转。
"""

from __future__ import annotations

from typing import Any

import structlog

from ai_engine.plan_generator import PlanGenerator
from ai_engine.signal_scorer import MIN_ACCEPTABLE_SCORE
from ai_engine.prompt_versioner import PromptVersioner

logger = structlog.get_logger(__name__)

# ─── 全局实例（惰性初始化）─────────────────────────────────────
_generator: PlanGenerator | None = None
_versioner: PromptVersioner | None = None

# 缓存最近收到的指标数据（按 symbol 和 timeframe）
# PlanGenerator.generate_plan() 需要指标数据 + 制度信号
_latest_indicators: dict[str, dict[str, float]] = {}


def _get_generator() -> PlanGenerator:
    global _generator
    if _generator is None:
        _generator = PlanGenerator()
    return _generator


def _get_versioner() -> PromptVersioner:
    global _versioner
    if _versioner is None:
        _versioner = PromptVersioner()
    return _versioner


async def process_regime_signal(message: dict[str, Any]) -> dict[str, Any] | None:
    """
    处理单条 regime_signal Stream 消息。

    1. 从消息中提取制度信号
    2. 组装需要的指标数据
    3. 调用 PlanGenerator.generate_plan()
    4. 转换为 ai_signal Stream 格式

    参数:
        message: regime_signal Stream 消息
            - symbol, ts, regime, confidence, adx, bb_width, close, ...

    返回:
        ai_signal Stream 消息，或 None（计划生成失败 / 降级为 FLAT）
    """
    symbol: str = message.get("symbol", "")
    regime: str = message.get("regime", "UNKNOWN")
    regime_confidence: float = message.get("confidence", 0.5)

    # ─── 构造 indicators_by_tf ────────────────────────────
    # AI 引擎需要多周期指标数据；当前仅收到单周期信号
    # 将 regime_signal 中携带的 close 转为简化的指标字典
    indicators_by_tf: dict[str, dict[str, float]] = {}

    close = message.get("close")
    if close is not None:
        indicators_by_tf["1h"] = {"close": float(close)}

    # 合并最近缓存的指标数据（来自 indicators Stream）
    # 实际生产环境中可扩展为完整的指标提交通道
    for tf, indics in _latest_indicators.items():
        if tf not in indicators_by_tf:
            indicators_by_tf[tf] = indics

    if not indicators_by_tf:
        logger.warning("无指标数据可用，跳过 AI 信号生成", symbol=symbol)
        return None

    # ─── 构造 regime_signal 字典 ──────────────────────────
    regime_signal: dict[str, Any] = {
        "symbol": symbol,
        "regime": regime,
        "confidence": regime_confidence,
        "adx": message.get("adx", 0.0),
        "bb_width": message.get("bb_width", 0.0),
    }

    # ─── 生成交易计划 ─────────────────────────────────────
    generator = _get_generator()
    versioner = _get_versioner()

    try:
        plan = await generator.generate_plan(
            indicators_by_tf=indicators_by_tf,
            regime_signal=regime_signal,
        )
    except Exception as exc:
        logger.error("交易计划生成异常", symbol=symbol, error=str(exc))
        return None

    if plan is None:
        logger.info("交易计划为空（降级/FALLBACK）", symbol=symbol)
        # 返回 FLAT 信号，让 risk_guardian 做最终判断
        return _build_flat_signal(symbol, regime, regime_confidence, message.get("ts", 0))

    # ─── 转换为 ai_signal 格式 ────────────────────────────
    prompt_version = versioner.get_version("market_analysis")
    signal = generator.to_signal(plan, prompt_version=prompt_version)

    # 补充元数据
    signal["symbol"] = symbol
    signal["ts"] = message.get("ts", 0)
    signal["regime"] = regime
    signal["score"] = plan.score if hasattr(plan, "score") else 0.0

    score_note = ""
    if hasattr(plan, "score") and plan.score is not None and plan.score < MIN_ACCEPTABLE_SCORE:
        score_note = "（低分信号）"

    logger.info(
        f"AI 信号生成完成{score_note}",
        symbol=symbol,
        direction=signal.get("direction", "N/A"),
        regime=regime,
        score=getattr(plan, "score", None),
        confidence=plan.confidence if hasattr(plan, "confidence") else None,
    )

    return signal


def cache_indicators(symbol: str, timeframe: str, indicators: dict[str, float]) -> None:
    """
    缓存指标数据供 AI 引擎使用。

    由 indicators Stream 的消费者在发布前调用，
    确保 AI 引擎有最新的多周期指标数据。

    参数:
        symbol: 交易对
        timeframe: 周期
        indicators: 指标字典
    """
    global _latest_indicators
    key = f"{symbol}_{timeframe}"
    _latest_indicators[key] = indicators
    logger.debug("指标数据已缓存", symbol=symbol, timeframe=timeframe, count=len(indicators))


def _build_flat_signal(
    symbol: str,
    regime: str,
    confidence: float,
    ts: int,
) -> dict[str, Any]:
    """构建 FLAT（无操作）信号。"""
    return {
        "symbol": symbol,
        "ts": ts,
        "direction": "FLAT",
        "confidence": 0.0,
        "regime": regime,
        "score": 0.0,
        "reason": "AI 引擎降级 / FLAT",
        "is_fallback": True,
    }


__all__ = ["process_regime_signal", "cache_indicators"]
