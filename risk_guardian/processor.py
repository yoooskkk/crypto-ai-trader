"""
模块名称: processor.py
所属层级: 风险控制层 (Risk Guardian)
输入来源: ai_signal Stream
输出去向: trade_order Stream（经风控审核后的最终交易指令）
关键依赖: signal_arbiter, circuit_breaker, position_sizer, drawdown_limit, exposure_monitor

ai_signal → 风控审核管道。

消费 ai_signal Stream，执行完整的风控检查链：
  1. 熔断器检查
  2. 回撤限制检查
  3. 仓位计算（Kelly + 制度乘数）
  4. 信号仲裁
输出 trade_order Stream 供 Freqtrade 策略执行。

铁律 #1：risk_guardian 是唯一可调用 Freqtrade force_exit API 的模块。
"""

from __future__ import annotations

from typing import Any

import structlog

from risk_guardian.circuit_breaker import CircuitBreaker
from risk_guardian.drawdown_limit import DrawdownLimit
from risk_guardian.exposure_monitor import ExposureMonitor
from risk_guardian.freqtrade_client import FreqtradeClient
from risk_guardian.position_sizer import PositionSizer
from risk_guardian.signal_arbiter import SignalArbiter
from observability.alert_manager import alert_manager

logger = structlog.get_logger(__name__)

# ─── 全局实例（惰性初始化）─────────────────────────────────────
_breaker: CircuitBreaker | None = None
_drawdown: DrawdownLimit | None = None
_exposure: ExposureMonitor | None = None
_freqtrade_client: FreqtradeClient | None = None
_sizer: PositionSizer | None = None
_arbiter: SignalArbiter | None = None


def _get_breaker() -> CircuitBreaker:
    global _breaker
    if _breaker is None:
        _breaker = CircuitBreaker()
    return _breaker


def _get_drawdown() -> DrawdownLimit:
    global _drawdown
    if _drawdown is None:
        _drawdown = DrawdownLimit()
    return _drawdown


def _get_freqtrade_client() -> FreqtradeClient:
    global _freqtrade_client
    if _freqtrade_client is None:
        _freqtrade_client = FreqtradeClient()
    return _freqtrade_client


def _get_exposure() -> ExposureMonitor:
    global _exposure
    if _exposure is None:
        _exposure = ExposureMonitor()
    return _exposure


def _get_sizer() -> PositionSizer:
    global _sizer
    if _sizer is None:
        _sizer = PositionSizer()
    return _sizer


def _get_arbiter() -> SignalArbiter:
    global _arbiter
    if _arbiter is None:
        _arbiter = SignalArbiter(
            circuit_breaker=_get_breaker(),
            drawdown_limit=_get_drawdown(),
            position_sizer=_get_sizer(),
        )
    return _arbiter


async def process_ai_signal(message: dict[str, Any]) -> dict[str, Any] | None:
    """
    处理单条 ai_signal Stream 消息。

    执行完整风控链：
      1. 提取信号内容
      2. 熔断器检查（是否允许开仓）
      3. 回撤检查
      4. 仓位计算
      5. SignalArbiter 仲裁

    参数:
        message: ai_signal Stream 消息
            - symbol, direction, confidence, score, regime, ts, ...

    返回:
        trade_order Stream 消息，或 None（风控拒绝时跳过）
    """
    symbol: str = message.get("symbol", "")
    direction: str = message.get("direction", "FLAT")
    confidence: float = message.get("confidence", 0.0)
    score: float = message.get("score", 0.0)
    regime: str = message.get("regime", "UNKNOWN")
    ts: int = message.get("ts", 0)
    is_fallback: bool = message.get("is_fallback", False)
    reason: str = message.get("reason", "")

    logger.info(
        "收到 AI 信号",
        symbol=symbol,
        direction=direction,
        confidence=confidence,
        score=score,
        regime=regime,
        is_fallback=is_fallback,
    )

    # ─── 调用 SignalArbiter 仲裁 ─────────────────────────
    arbiter = _get_arbiter()

    try:
        # 构建 ai_signal 字典（SignalArbiter.arbitrate 格式）
        ai_signal_payload: dict[str, Any] = {
            "symbol": symbol,
            "direction": direction,
            "confidence": confidence,
            "regime": regime,
            "score": score,
            "reasoning": reason,
        }

        # 更新权益（熔断器内部状态 & 回撤追踪）
        current_equity = message.get("current_equity", 10000.0)
        _get_breaker().update_equity(current_equity)

        # 检查回撤状态，如果触发强平等级则调用 API
        dd_status = _get_drawdown().check_limits()
        if dd_status.get("force_exit"):
            logger.critical(
                "回撤触发强平等级",
                symbol=symbol,
                level=dd_status["level"],
                drawdown_pct=dd_status["drawdown_pct"],
            )
            _call_force_exit(symbol, f"回撤 {dd_status['level']}: {dd_status['drawdown_pct']}%")

        # SignalArbiter.arbitrate() 始终返回 ArbitratedOrder
        # （内部已包含熔断器、回撤、仓位计算）
        arbitrated = arbiter.arbitrate(
            ai_signal=ai_signal_payload,
            ts=ts,
            regime=regime,
        )

        # 转换为 Stream 消息格式
        order = arbitrated.to_stream_message()

        # 补充元数据
        order["score"] = score
        order["is_fallback"] = is_fallback
        order["regime"] = regime

        if arbitrated.action == "FLAT":
            logger.info(
                "风控过滤信号",
                symbol=symbol,
                direction=direction,
                reason=arbitrated.reasoning,
            )
        else:
            logger.info(
                "风控审核通过",
                symbol=symbol,
                action=arbitrated.action,
                size_pct=arbitrated.size_pct,
                audit_id=arbitrated.audit_id,
            )

        return order

    except Exception as exc:
        logger.error("风控审核异常", symbol=symbol, error=str(exc))
        return _build_rejected_signal(
            symbol, direction, regime, ts,
            reason=f"风控异常: {exc}",
        )


def _build_rejected_signal(
    symbol: str,
    direction: str,
    regime: str,
    ts: int,
    reason: str = "",
) -> dict[str, Any]:
    """构建被风控拒绝的 trade_order（FLAT）。"""
    return {
        "symbol": symbol,
        "ts": ts,
        "direction": "FLAT",
        "confidence": 0.0,
        "score": 0.0,
        "regime": regime,
        "position_size_pct": 0.0,
        "stop_loss_pct": 0.0,
        "take_profit_pct": 0.0,
        "reason": reason,
        "is_fallback": True,
    }


def _call_force_exit(symbol: str, reason: str) -> None:
    """调用 Freqtrade REST API 强平所有持仓。"""
    try:
        # 发送告警
        try:
            import asyncio
            asyncio.ensure_future(alert_manager.critical(
                "回撤触发强制平仓",
                detail=reason,
                symbol=symbol,
                tags={"reason": reason},
            ))
        except Exception:
            pass

        client = _get_freqtrade_client()
        result = client.force_exit_all()
        if result.success:
            logger.critical("强平成功", symbol=symbol, reason=reason)
        else:
            logger.error("强平失败", symbol=symbol, reason=reason, error=result.error)
    except Exception as exc:
        logger.error("强平调用异常", symbol=symbol, reason=reason, error=str(exc))


def reset_instances() -> None:
    """重置所有全局实例（用于测试）。"""
    global _breaker, _drawdown, _exposure, _sizer, _arbiter, _freqtrade_client
    _breaker = None
    _drawdown = None
    _exposure = None
    _sizer = None
    _arbiter = None
    _freqtrade_client = None
    logger.info("风控模块全局实例已重置")


__all__ = ["process_ai_signal", "reset_instances"]
