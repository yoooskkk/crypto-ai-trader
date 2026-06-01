"""
模块名称: news_integrator.py
所属层级: 分析层 (Analysis)
输入来源: news_scraper + sentiment_feed（Redis）+ TradePlan
输出去向: 调整后的置信度权重（confidence 修正系数）
关键依赖: structlog

新闻情绪与技术分析融合模块。
将新闻情绪评分和 Fear & Greed 指数与 AI 信号置信度进行加权融合。
新闻属于可选增强，当数据不可用时平滑降级。

修订记录:
- v1.0: 初始实现，双通道情绪融合 + 置信度调整
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from ai_engine.schema_validator import TradePlan, Direction
from data.sentiment_feed import SentimentReading

logger = structlog.get_logger(__name__)

# ─── 常量 ───────────────────────────────────────────────────────

NEWS_SENTIMENT_WEIGHT: float = 0.30
FG_WEIGHT: float = 0.20
BASE_CONFIDENCE_WEIGHT: float = 0.50

FEAR_THRESHOLD = 25
GREED_THRESHOLD = 75


# ─── 数据结构 ───────────────────────────────────────────────────────


@dataclass
class SentimentSignal:
    """融合后的情绪信号"""
    adjusted_confidence: float
    news_score: float
    fear_greed_score: float
    news_count: int
    fg_value: int | None
    is_extreme: bool
    override_to_flat: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "adjusted_confidence": round(self.adjusted_confidence, 4),
            "news_score": round(self.news_score, 4),
            "fear_greed_score": round(self.fear_greed_score, 4),
            "news_count": self.news_count,
            "fg_value": self.fg_value,
            "is_extreme": self.is_extreme,
            "override_to_flat": self.override_to_flat,
        }


class NewsIntegrator:
    """
    新闻情绪与分析信号融合器。

    用法:
        integrator = NewsIntegrator()
        signal = await integrator.integrate(
            base_confidence=0.8,
            news_items=[...],
            fear_greed=SentimentReading(...),
        )
    """

    def __init__(
        self,
        news_weight: float = NEWS_SENTIMENT_WEIGHT,
        fg_weight: float = FG_WEIGHT,
        base_weight: float = BASE_CONFIDENCE_WEIGHT,
        extreme_override: bool = True,
    ):
        total = news_weight + fg_weight + base_weight
        self._w_news = news_weight / total
        self._w_fg = fg_weight / total
        self._w_base = base_weight / total
        self._extreme_override = extreme_override

    async def integrate(
        self,
        base_confidence: float,
        news_items: list[Any] | None = None,
        fear_greed: SentimentReading | None = None,
        current_price: float | None = None,
        symbol: str | None = None,
    ) -> SentimentSignal:
        """
        融合新闻情绪与技术信号。

        参数:
            base_confidence: AI 原始置信度 (0.0~1.0)
            news_items: 新闻条目列表（需有 sentiment_score 属性）
            fear_greed: 最新 Fear & Greed 读数

        返回:
            SentimentSignal
        """
        news_score, news_count = self._score_news(news_items)
        fg_score, fg_value, is_extreme = self._score_fg(fear_greed)

        adjusted = (
            self._w_base * base_confidence
            + self._w_news * news_score
            + self._w_fg * fg_score
        )
        adjusted = max(0.0, min(1.0, adjusted))

        override_to_flat = False
        if is_extreme and self._extreme_override:
            adjusted *= 0.5
            if adjusted < 0.3:
                override_to_flat = True
            logger.info("极端情绪覆写", fg_value=fg_value, override_to_flat=override_to_flat)

        return SentimentSignal(
            adjusted_confidence=adjusted,
            news_score=news_score,
            fear_greed_score=fg_score,
            news_count=news_count,
            fg_value=fg_value,
            is_extreme=is_extreme,
            override_to_flat=override_to_flat,
        )

    @staticmethod
    def _score_news(items: list[Any] | None) -> tuple[float, int]:
        """新闻情绪 → [0,1] 评分"""
        if not items:
            return 0.5, 0
        scores = [
            max(-1.0, min(1.0, float(s)))
            for item in items
            if (s := getattr(item, "sentiment_score", None)) is not None
            and isinstance(s, (int, float))
        ]
        if not scores:
            return 0.5, 0
        n = len(scores)
        weights = [1.0 + 0.1 * (n - i) for i in range(n)]
        avg = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
        return max(0.0, min(1.0, (avg + 1.0) / 2.0)), n

    @staticmethod
    def _score_fg(
        fear_greed: SentimentReading | None,
    ) -> tuple[float, int | None, bool]:
        if fear_greed is None:
            return 0.5, None, False
        v = getattr(fear_greed, "value", 50)
        if v is None:
            return 0.5, None, False
        return v / 100.0, v, v <= FEAR_THRESHOLD or v >= GREED_THRESHOLD

    @staticmethod
    def adjust_plan(base: TradePlan, signal: SentimentSignal) -> TradePlan:
        """将情绪信号写回 TradePlan（返回副本）"""
        from copy import deepcopy
        plan = deepcopy(base)
        if signal.override_to_flat:
            plan.direction = Direction.FLAT
            plan.confidence = 0.1
            plan.reasoning += (
                "\n[情绪覆写] 极端情绪，强制 FLAT。"
                f"F&G={signal.fg_value}"
            )
        else:
            plan.confidence = signal.adjusted_confidence
            plan.reasoning += (
                f"\n[情绪融合] confidence={signal.adjusted_confidence:.2f}, "
                f"news={signal.news_count}条, F&G={signal.fg_value}"
            )
        return plan


__all__ = ["NewsIntegrator", "SentimentSignal"]

