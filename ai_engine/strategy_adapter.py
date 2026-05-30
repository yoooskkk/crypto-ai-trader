"""
模块名称: strategy_adapter.py
所属层级: AI 引擎层 (AI Engine)
输入来源: TradePlan（来自 plan_generator）
输出去向: Freqtrade enter_long/enter_short 信号字典
关键依赖: schema_validator（TradePlan）, structlog

将 TradePlan 转换为 Freqtrade 策略可消费的信号格式。
适配 Freqtrade 的 enter_long / enter_short / exit_long / exit_short API。

修订记录:
- v1.0: 初始实现，TradePlan → Freqtrade 信号适配
"""

from __future__ import annotations

from typing import Any

import structlog

from ai_engine.schema_validator import TradePlan, Direction

logger = structlog.get_logger(__name__)

# ─── 常量 ─────────────────────────────────────────────────────

# ai_signal Stream 格式的键名映射
SIGNAL_KEYS = {
    "symbol": "symbol",
    "direction": "direction",
    "confidence": "confidence",
    "entry": "entry",
    "sl": "sl",
    "tp": "tp",
    "score": "score",
    "prompt_version": "prompt_version",
    "regime": "regime",
    "reasoning": "reasoning",
    "is_fallback": "is_fallback",
}


class StrategyAdapter:
    """
    Freqtrade 策略信号适配器。

    将 TradePlan（由 LLM 生成、schema_validator 校验）转换为
    Freqtrade enter_long/enter_short/exit 信号字典。

    用法:
        adapter = StrategyAdapter()
        signal = adapter.to_freqtrade_signal(plan, meta={"prompt_version": "abc123"})
        # signal 可被 Freqtrade 策略直接消费
    """

    def to_freqtrade_signal(
        self,
        plan: TradePlan,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        将 TradePlan 转换为 Freqtrade 信号。

        Freqtrade 期望的 DataFrame 列（信号列）:
          - enter_long / enter_short: 1 或 0
          - exit_long / exit_short: 1 或 0
          - sl_entry / sl_exit: 止损价
          - tp_entry / tp_exit: 止盈价

        同时返回 ai_signal Stream 格式的完整信号。

        参数:
            plan: 校验通过的 TradePlan
            meta: 附加元数据（prompt_version, score, is_fallback 等）

        返回:
            {
                "freqtrade": {
                    "enter_long": 0 | 1,
                    "enter_short": 0 | 1,
                    "exit_long": 0 | 1,
                    "exit_short": 0 | 1,
                    "sl_entry": float | None,
                    "tp_entry": float | None,
                },
                "ai_signal": {  # 符合 STREAM_SCHEMA.md 的 ai_signal 格式
                    "symbol": ...
                    "direction": "LONG" | "SHORT" | "FLAT",
                    "ts": int,
                    "confidence": float,
                    "entry": float | None,
                    "sl": float | None,
                    "tp": float | None,
                    "score": float,
                    "prompt_version": str,
                    "regime": str,
                    "reasoning": str,
                    "is_fallback": bool,
                },
            }
        """
        meta = meta or {}
        direction_upper = plan.direction.value.upper()

        # 构建 Freqtrade 信号
        freqtrade_signal: dict[str, Any] = {
            "enter_long": 0,
            "enter_short": 0,
            "exit_long": 0,
            "exit_short": 0,
            "sl_entry": plan.stop_loss,
            "tp_entry": plan.take_profit,
        }

        if direction_upper == "LONG":
            freqtrade_signal["enter_long"] = 1
            freqtrade_signal["exit_short"] = 1  # 平空
        elif direction_upper == "SHORT":
            freqtrade_signal["enter_short"] = 1
            freqtrade_signal["exit_long"] = 1  # 平多
        # FLAT: 不清除已有仓位（由 risk_guardian 决定）

        # 构建 ai_signal Stream 格式
        import time
        ai_signal: dict[str, Any] = {
            "symbol": plan.symbol,
            "ts": int(time.time() * 1000),
            "direction": direction_upper,
            "confidence": plan.confidence,
            "entry": plan.entry_price,
            "sl": plan.stop_loss,
            "tp": plan.take_profit,
            "score": plan.score if hasattr(plan, "score") else 0.0,
            "prompt_version": meta.get("prompt_version", "unknown"),
            "regime": plan.regime,
            "reasoning": plan.reasoning,
            "is_fallback": meta.get("is_fallback", False),
        }

        result = {
            "freqtrade": freqtrade_signal,
            "ai_signal": ai_signal,
        }

        logger.debug(
            "策略信号转换完成",
            symbol=plan.symbol,
            direction=direction_upper,
            confidence=plan.confidence,
            score=plan.score,
            is_fallback=meta.get("is_fallback", False),
        )

        return result

    @staticmethod
    def to_stream_message(
        plan: TradePlan,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        仅返回 ai_signal Stream 格式消息（不含 Freqtrade 信号）。
        用于发布到 Redis Stream。
        """
        adapter = StrategyAdapter()
        result = adapter.to_freqtrade_signal(plan, meta)
        return result["ai_signal"]


__all__ = ["StrategyAdapter"]
