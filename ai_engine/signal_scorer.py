"""
模块名称: signal_scorer.py
所属层级: AI 引擎层 (AI Engine)
输入来源: TradePlan + regime_signal + multi_tf_trend 共识
输出去向: float 分数（0.0 ~ 1.0），写入 TradePlan.score
关键依赖: structlog, strategy_switcher（用于查制度覆盖值）

对 AI 信号进行综合评分。
评分维度：
1. AI 置信度 × 制度匹配度
2. 多周期共识强度
3. 入场时机合理性

修订记录:
- v1.0: 初始实现，三维评分 + 制度加权
"""

from __future__ import annotations

from typing import Any

import structlog

from ai_engine.schema_validator import TradePlan, Direction

logger = structlog.get_logger(__name__)

# ─── 评分权重 ─────────────────────────────────────────────────

# AI 置信度权重
W_AI_CONFIDENCE: float = 0.40
# 制度匹配度权重
W_REGIME_MATCH: float = 0.30
# 多周期共识强度权重
W_CONSENSUS: float = 0.30

# 最低可接受总分（低于此分，plan_generator 应拒绝）
MIN_ACCEPTABLE_SCORE: float = 0.35


class SignalScorer:
    """
    AI 信号评分器。

    对 TradePlan 进行三维综合评分：
    1. AI 置信度 (confidence) — LLM 自评的可信度
    2. 制度匹配度 — 信号方向与当前制度的匹配程度
    3. 多周期共识强度 — 多个时间框架的一致性

    用法:
        scorer = SignalScorer()
        score = scorer.score(trade_plan, regime_data, trend_consensus)
    """

    def __init__(
        self,
        weights: dict[str, float] | None = None,
    ):
        """
        初始化评分器。

        参数:
            weights: 自定义权重，默认使用模块级常量
        """
        self._weights = weights or {
            "ai_confidence": W_AI_CONFIDENCE,
            "regime_match": W_REGIME_MATCH,
            "consensus": W_CONSENSUS,
        }

    def score(
        self,
        plan: TradePlan,
        regime_data: dict[str, Any],
        trend_consensus: dict[str, Any] | None = None,
    ) -> float:
        """
        对 TradePlan 进行综合评分。

        参数:
            plan: 待评分的交易计划
            regime_data: 制度数据，包含:
                - regime: str（当前市场制度）
                - confidence: float（制度置信度）
                - overrides: dict（来自 strategy_switcher 的覆盖参数）
                  可选字段：min_confidence, disable_trend_signals 等
            trend_consensus: 多周期共识数据（来自 multi_tf_trend）
                包含:
                - direction: str
                - strength: str ("STRONG" | "WEAK")
                可选

        返回:
            综合分数 (0.0 ~ 1.0)
        """
        # FLAT 信号直接给中间分，不参与详细评分
        if plan.direction == Direction.FLAT:
            base_score = 0.5
            logger.debug("FLAT 信号评分", score=base_score)
            return base_score

        regime = regime_data.get("regime", "UNKNOWN")
        regime_confidence = regime_data.get("confidence", 0.5)
        overrides = regime_data.get("overrides", {})

        # 1. AI 置信度评分
        ai_score = self._score_ai_confidence(
            plan.confidence,
            overrides.get("min_confidence", 0.65),
        )

        # 2. 制度匹配度评分
        regime_score = self._score_regime_match(
            plan.direction.value.upper(),
            regime,
            regime_confidence,
            overrides,
        )

        # 3. 多周期共识评分
        consensus_score = self._score_consensus(
            plan.direction.value.upper(),
            trend_consensus,
        )

        # 加权综合
        total = (
            self._weights["ai_confidence"] * ai_score
            + self._weights["regime_match"] * regime_score
            + self._weights["consensus"] * consensus_score
        )

        # 惩罚条款：制度不匹配时降低总分
        if regime_score < 0.3:
            total *= 0.7
            logger.debug(
                "制度不匹配惩罚",
                regime=regime,
                direction=plan.direction.value.upper(),
                regime_score=regime_score,
                penalty=0.7,
            )

        score = max(0.0, min(1.0, total))

        logger.debug(
            "信号评分完成",
            direction=plan.direction.value.upper(),
            ai_score=ai_score,
            regime_score=regime_score,
            consensus_score=consensus_score,
            total_score=score,
        )

        return score

    @staticmethod
    def _score_ai_confidence(
        confidence: float,
        min_confidence: float,
    ) -> float:
        """
        AI 置信度评分。

        - >= min_confidence: 线性映射到 0.6 ~ 1.0
        - < min_confidence: 线性映射到 0.0 ~ 0.6
        """
        if confidence >= min_confidence:
            # 超额部分加分
            excess = (confidence - min_confidence) / (1.0 - min_confidence)
            return 0.6 + 0.4 * min(excess, 1.0)
        else:
            # 不足部分减分
            return max(0.0, confidence / min_confidence * 0.6)

    @staticmethod
    def _score_regime_match(
        direction: str,
        regime: str,
        regime_confidence: float,
        overrides: dict[str, Any],
    ) -> float:
        """
        制度匹配度评分。

        根据 ARCH.md 第7节的制度联动规则:
          TRENDING      → 做多/做空均可
          RANGING       → 做多/做空均可（轻仓）
          HIGH_VOLATILITY → 方向受限，高波动时倾向不交易
          UNKNOWN       → FLAT 优先，方向信号保守

        如果 disable_trend_signals=True，则方向信号分数减半。
        """
        disable_trend = overrides.get("disable_trend_signals", False)

        # 基础分：制度置信度
        base = regime_confidence

        # 制度特定调整
        if regime == "UNKNOWN":
            # UNKNOWN 时，任何方向信号都应保守
            base *= 0.5
        elif regime == "HIGH_VOLATILITY":
            # 高波动时，趋向保守
            base *= 0.7
        elif regime == "RANGING":
            # 震荡市，方向信号得分不变
            pass
        elif regime == "TRENDING":
            # 趋势市，方向信号加分
            base = min(1.0, base * 1.2)

        # 如果关闭了趋势信号（如 RANGING 时），降低分数
        if disable_trend:
            base *= 0.5
            logger.debug(
                "趋势信号已禁用，制度匹配度减半",
                regime=regime,
                adjusted_score=base,
            )

        return max(0.0, min(1.0, base))

    @staticmethod
    def _score_consensus(
        direction: str,
        trend_consensus: dict[str, Any] | None,
    ) -> float:
        """
        多周期共识评分。

        规则:
          - 共识方向与信号方向一致 + STRONG = 高分 (0.9~1.0)
          - 共识方向与信号方向一致 + WEAK  = 中分 (0.6~0.8)
          - 无共识数据                          = 中分 (0.5)
          - 共识方向与信号方向不一致             = 低分 (0.0~0.3)
        """
        if trend_consensus is None:
            return 0.5

        consensus_dir = trend_consensus.get("direction", "FLAT")
        strength = trend_consensus.get("strength", "WEAK")

        if consensus_dir == direction:
            if strength == "STRONG":
                return 0.95
            else:
                return 0.70
        elif consensus_dir == "FLAT":
            # 共识为 FLAT 但信号有方向 = 风险较高
            return 0.30
        else:
            # 共识方向与信号方向相反 = 极低分
            return 0.10


__all__ = ["SignalScorer", "MIN_ACCEPTABLE_SCORE"]
