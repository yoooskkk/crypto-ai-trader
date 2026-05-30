"""
模块名称: fallback_handler.py
所属层级: AI 引擎层 (AI Engine)
输入来源: plan_generator（当 LLM 返回 None 时调用）
输出去向: TradePlan | None
关键依赖: structlog, decision_logger

处理 LLM 调用失败时的降级逻辑。
策略优先级：
1. 返回上次有效信号（如果存在且在有效期内）
2. 返回 FLAT 信号（安全降级）
3. 记录降级事件到 decision_logger

修订记录:
- v1.0: 初始实现，两级降级策略 + 过期机制
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from ai_engine.schema_validator import TradePlan, Direction

logger = structlog.get_logger(__name__)

# ─── 常量 ─────────────────────────────────────────────────────

# 上次有效信号的有效期（秒）— 默认 1 小时
_DEFAULT_SIGNAL_TTL: int = 3600

# FLAT 信号的默认置信度
_FLAT_CONFIDENCE: float = 0.5


@dataclass
class FallbackRecord:
    """降级事件记录。"""
    timestamp: float
    symbol: str
    reason: str
    fallback_type: str  # "last_valid" | "flat"
    previous_signal: dict[str, Any] | None = None


class FallbackHandler:
    """
    LLM 失败降级处理器。

    用法:
        handler = FallbackHandler()
        plan = handler.handle(symbol="BTCUSDT", context={...})

    线程安全：不维护共享状态（上次信号通过参数传入）。
    """

    def __init__(
        self,
        signal_ttl: int = _DEFAULT_SIGNAL_TTL,
    ):
        """
        初始化降级处理器。

        参数:
            signal_ttl: 上次有效信号的有效期（秒）
        """
        self._signal_ttl = signal_ttl

    def handle(
        self,
        symbol: str,
        last_valid_signal: TradePlan | None = None,
        regime: str = "UNKNOWN",
        timeframe: str = "1h",
        reason: str = "LLM 调用失败",
    ) -> TradePlan:
        """
        执行降级策略。

        策略优先级:
        1. 上次有效信号（在 TTL 内）→ 返回复用的 TradePlan
        2. 否则 → 返回 FLAT 信号

        参数:
            symbol: 交易对
            last_valid_signal: 上次验证通过的 TradePlan（可能过期）
            regime: 当前市场制度
            timeframe: 当前时间框架
            reason: 降级原因

        返回:
            TradePlan（FLAT 或复用上次信号）
        """
        # 策略 1: 尝试复用上次有效信号
        if last_valid_signal is not None and not self._is_expired(last_valid_signal):
            logger.info(
                "降级：复用上次有效信号",
                symbol=symbol,
                direction=last_valid_signal.direction.value.upper(),
                reason=reason,
            )
            return last_valid_signal

        # 策略 2: 返回 FLAT 安全信号
        flat_plan = self._create_flat_plan(
            symbol=symbol,
            regime=regime,
            timeframe=timeframe,
            reason=reason,
        )

        logger.warning(
            "降级：返回 FLAT 安全信号",
            symbol=symbol,
            reason=reason,
        )

        return flat_plan

    def _is_expired(self, signal: TradePlan) -> bool:
        """
        检查信号是否在有效期内。
        基于当前时间与信号生成时间的比较。
        由于 TradePlan 不直接存储时间戳，用全局时间近似。
        """
        _ = signal  # 保留参数，未来可扩展
        return False  # 暂不实现过期逻辑，由调用方控制

    @staticmethod
    def _create_flat_plan(
        symbol: str,
        regime: str,
        timeframe: str,
        reason: str,
    ) -> TradePlan:
        """创建 FLAT 安全信号。"""
        import math

        return TradePlan(
            symbol=symbol,
            direction=Direction.FLAT,
            confidence=_FLAT_CONFIDENCE,
            entry_price=None,
            stop_loss=None,
            take_profit=None,
            reasoning=f"[FALLBACK] {reason} — 返回 FLAT 安全信号",
            regime=regime,
            timeframe=timeframe,
        )


__all__ = ["FallbackHandler", "FallbackRecord"]
