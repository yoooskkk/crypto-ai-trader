"""
模块名称: plan_generator.py
所属层级: AI 引擎层 (AI Engine)
输入来源: indicators Stream + regime_signal Stream
输出去向: ai_signal Stream（经 risk_guardian 审核后到 trade_order）
关键依赖: prompt_builder, llm_client, schema_validator, signal_scorer, strategy_adapter, fallback_handler

AI 交易计划生成器。
串联 prompt_builder → llm_client → schema_validator → signal_scorer → strategy_adapter 流程。
必须严格遵守 ROLE_ANALYSIS.md 定义的调用顺序，不得跳过任何步骤。

铁律 #5：LLM 输出必须经 schema_validator.py 校验后才能流转，不得绕过。

修订记录:
- v1.0: 初始实现，完整串联流程 + 降级 + 决策日志
"""

from __future__ import annotations

from typing import Any

import structlog

from analysis.prompt_builder import PromptBuilder
from analysis.multi_tf_trend import build_trend_summary

from ai_engine.llm_client import LLMClient
from ai_engine.schema_validator import TradePlan, Direction, parse_trade_plan
from ai_engine.signal_scorer import SignalScorer, MIN_ACCEPTABLE_SCORE
from ai_engine.strategy_adapter import StrategyAdapter
from ai_engine.fallback_handler import FallbackHandler

logger = structlog.get_logger(__name__)


class PlanGenerator:
    """
    AI 交易计划生成器。

    职责:
    1. 接收指标 + 制度信号 → 构建 Prompt
    2. 调用 LLM → 获取原始输出
    3. Schema 校验 → 解析为 TradePlan
    4. 评分 + 记录决策 → 输出带信号的字典

    用法:
        gen = PlanGenerator()
        plan = await gen.generate_plan(indicators_by_tf, regime_signal)
        if plan:
            signal = gen.to_signal(plan, trend_consensus={...})
    """

    def __init__(
        self,
        prompt_builder: PromptBuilder | None = None,
        llm_client: LLMClient | None = None,
        signal_scorer: SignalScorer | None = None,
        strategy_adapter: StrategyAdapter | None = None,
        fallback_handler: FallbackHandler | None = None,
    ):
        """
        初始化计划生成器。

        所有依赖均有默认实例，可传入自定义实例用于测试。
        """
        self._prompt_builder = prompt_builder or PromptBuilder()
        self._llm_client = llm_client or LLMClient()
        self._signal_scorer = signal_scorer or SignalScorer()
        self._strategy_adapter = strategy_adapter or StrategyAdapter()
        self._fallback_handler = fallback_handler or FallbackHandler()

        # 缓存上次有效信号，用于 fallback
        self._last_valid_signal: TradePlan | None = None

    async def generate_plan(
        self,
        indicators_by_tf: dict[str, dict[str, float]],
        regime_signal: dict[str, Any],
    ) -> TradePlan | None:
        """
        生成交易计划（完整串联流程）。

        必须按此顺序，不得跳过任何步骤:
        1. 构建 Prompt
        2. 记录 Prompt 版本
        3. 调用 LLM（已有重试逻辑）
        4. Schema 校验（必须，不得绕过）
        5. 评分
        6. 记录决策

        参数:
            indicators_by_tf: {timeframe: {indicator_name: value}}
            regime_signal: regime_signal Stream 消息
                - 至少包含: symbol, regime, confidence
                - 可选包含: adx, bb_width, method, prev_regime

        返回:
            TradePlan（通过校验且评分达标），或 None（严重错误时）
        """
        symbol = regime_signal.get("symbol", "UNKNOWN")
        regime = regime_signal.get("regime", "UNKNOWN")

        # ─── 步骤 1: 构建 Prompt ─────────────────────────────
        prompt = await self._prompt_builder.build(indicators_by_tf, regime_signal)
        if prompt is None:
            logger.error("Prompt 构建失败", symbol=symbol, regime=regime)
            return self._fallback_handler.handle(
                symbol=symbol,
                last_valid_signal=self._last_valid_signal,
                regime=regime,
                reason="Prompt 构建失败",
            )

        # ─── 步骤 2: 记录 Prompt 版本 ─────────────────────────
        from ai_engine.prompt_versioner import PromptVersioner
        versioner = PromptVersioner()
        template_name = self._prompt_builder.template_name
        prompt_version = versioner.get_version(template_name)

        logger.debug(
            "Prompt 构建成功",
            symbol=symbol,
            prompt_version=prompt_version,
            template=template_name,
            prompt_length=len(prompt),
        )

        # ─── 步骤 3: 调用 LLM ────────────────────────────────
        system = "你是加密货币量化交易员。请严格按 JSON 格式输出交易计划。"
        raw = await self._llm_client.complete(prompt, system=system)

        if raw is None:
            logger.warning("LLM 返回 None，触发降级", symbol=symbol)
            decision_logger = self._get_decision_logger()
            decision_logger.log(
                ts=__import__("time").time(),
                symbol=symbol,
                timeframe="1h",
                prompt_version=prompt_version,
                regime=regime,
                raw_llm_output="",
                validated=False,
                direction=None,
                confidence=None,
                breaker_state="CLOSED",
                signal_sent=False,
            )
            return self._fallback_handler.handle(
                symbol=symbol,
                last_valid_signal=self._last_valid_signal,
                regime=regime,
                reason="LLM 调用失败",
            )

        # ─── 步骤 4: Schema 校验（铁律 #5）────────────────────
        plan = parse_trade_plan(raw)

        if plan is None:
            logger.warning("Schema 校验失败", symbol=symbol, prompt_version=prompt_version)
            decision_logger = self._get_decision_logger()
            decision_logger.log(
                ts=__import__("time").time(),
                symbol=symbol,
                timeframe="1h",
                prompt_version=prompt_version,
                regime=regime,
                raw_llm_output=raw,
                validated=False,
                direction=None,
                confidence=None,
                breaker_state="CLOSED",
                signal_sent=False,
            )
            return self._fallback_handler.handle(
                symbol=symbol,
                last_valid_signal=self._last_valid_signal,
                regime=regime,
                reason=f"Schema 校验失败: LLM 输出格式不合法",
            )

        # 同步 plan 中的 symbol/regime（LLM 可能返回不同值）
        plan.symbol = symbol
        plan.regime = regime

        # ─── 步骤 5: 评分 ────────────────────────────────────
        trend_consensus = self._build_trend_consensus(indicators_by_tf, regime)
        regime_data = {
            "regime": regime,
            "confidence": regime_signal.get("confidence", 0.5),
            "overrides": regime_signal.get("overrides", {}),
        }
        score = self._signal_scorer.score(plan, regime_data, trend_consensus)
        plan.score = score

        # 如果评分低于阈值，仍然返回 plan（由下游 risk_guardian 做最终裁定）
        if score < MIN_ACCEPTABLE_SCORE:
            logger.warning(
                "信号评分低于阈值",
                symbol=symbol,
                score=score,
                threshold=MIN_ACCEPTABLE_SCORE,
                direction=plan.direction.value.upper(),
            )

        # ─── 步骤 6: 缓存有效信号 ────────────────────────────
        if plan.direction != Direction.FLAT:
            self._last_valid_signal = plan

        # ─── 步骤 7: 记录决策 ────────────────────────────────
        decision_logger = self._get_decision_logger()
        decision_logger.log(
            ts=__import__("time").time(),
            symbol=symbol,
            timeframe="1h",
            prompt_version=prompt_version,
            regime=regime,
            raw_llm_output=raw,
            validated=True,
            direction=plan.direction.value.upper(),
            confidence=plan.confidence,
            breaker_state="CLOSED",
            signal_sent=True,
        )

        logger.info(
            "交易计划生成成功",
            symbol=symbol,
            direction=plan.direction.value.upper(),
            confidence=plan.confidence,
            score=score,
            prompt_version=prompt_version,
        )

        return plan

    @staticmethod
    def _build_trend_consensus(
        indicators_by_tf: dict[str, dict[str, float]],
        regime: str,
    ) -> dict[str, Any] | None:
        """构建多周期趋势共识（供 signal_scorer 使用）。"""
        if not indicators_by_tf:
            return None
        try:
            summary = build_trend_summary(indicators_by_tf, regime)
            return summary.get("consensus")
        except Exception as exc:
            logger.warning("趋势共识计算失败", error=str(exc))
            return None

    @staticmethod
    def _get_decision_logger() -> Any:
        """获取决策日志器实例。"""
        from observability.decision_logger import DecisionLogger, DecisionRecord
        return _DecisionLoggerProxy(DecisionLogger())

    def to_signal(
        self,
        plan: TradePlan,
        prompt_version: str = "unknown",
        is_fallback: bool = False,
    ) -> dict[str, Any]:
        """
        将 TradePlan 转换为 ai_signal Stream 格式。
        用于发布到 Redis Stream。
        """
        meta = {
            "prompt_version": prompt_version,
            "is_fallback": is_fallback,
        }
        return self._strategy_adapter.to_stream_message(plan, meta)

    def reset_last_valid_signal(self) -> None:
        """重置上次有效信号缓存（用于测试）。"""
        self._last_valid_signal = None

    @property
    def last_valid_signal(self) -> TradePlan | None:
        return self._last_valid_signal


class _DecisionLoggerProxy:
    """
    决策日志代理。
    将 plan_generator 的简化接口适配到 DecisionLogger 的 DecisionRecord 模型。
    """

    def __init__(self, logger_impl: Any):
        self._impl = logger_impl

    def log(
        self,
        ts: float,
        symbol: str,
        timeframe: str,
        prompt_version: str,
        regime: str,
        raw_llm_output: str,
        validated: bool,
        direction: str | None,
        confidence: float | None,
        breaker_state: str,
        signal_sent: bool,
    ) -> None:
        """记录决策日志。"""
        from datetime import datetime, timezone
        from observability.decision_logger import DecisionRecord

        record = DecisionRecord(
            ts=datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            symbol=symbol,
            timeframe=timeframe,
            prompt_version=prompt_version,
            regime=regime,
            raw_llm_output=raw_llm_output,
            validated=validated,
            direction=direction,
            confidence=confidence,
            breaker_state=breaker_state,
            signal_sent=signal_sent,
        )
        self._impl.log(record)


__all__ = ["PlanGenerator"]
