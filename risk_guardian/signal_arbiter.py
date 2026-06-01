"""
模块名称: signal_arbiter.py
所属层级: 风险控制层 (Risk Guardian)
输入来源: ai_signal Stream（AI 信号）+ Freqtrade 内置信号（可选）
输出去向: trade_order Stream（最终交易指令）
关键依赖: risk_guardian/circuit_breaker.py, risk_guardian/drawdown_limit.py, config/risk.yml

AI 信号 vs Freqtrade 内置信号冲突仲裁器。

仲裁规则（固定规则，来自 ARCH.md 和 ROLE_RISK.md）：
  1. 熔断器 OPEN → 直接返回 FLAT（不开新仓）
  2. AI 置信度 > 0.8 且 direction != FLAT → AI 信号优先
  3. 否则 → Freqtrade 内置信号

写入 trade_order Stream 前必须检查：
  - circuit_breaker.is_closed()
  - drawdown_limit.can_open_position()

铁律 #1：risk_guardian 是唯一可调用 Freqtrade force_exit API 的模块。
铁律 #7：服务间通信只通过 Redis Stream，禁止 HTTP 同步调用。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import structlog
import yaml

from risk_guardian.circuit_breaker import CircuitBreaker, BreakerState
from risk_guardian.drawdown_limit import DrawdownLimit
from risk_guardian.position_sizer import PositionSizer, _load_signal_config

logger = structlog.get_logger(__name__)


# ─── 配置路径 ────────────────────────────────────

_CONFIG_DIR = Path(__file__).parent.parent / "config"
RISK_CONFIG_PATH = _CONFIG_DIR / "risk.yml"


# ─── 仲裁结果模型 ────────────────────────────────

@dataclass
class ArbitratedOrder:
    """
    仲裁后的最终交易指令（对应 trade_order Stream 格式）。
    """
    symbol: str
    ts: int                         # 毫秒时间戳
    action: str                     # LONG / SHORT / FLAT / FORCE_EXIT
    size_pct: float                 # 仓位占总资产比例
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    source: str = "ai_signal"      # ai_signal / freqtrade_native
    breaker_state: str = "CLOSED"
    audit_id: str = ""
    reasoning: str = ""

    def __post_init__(self) -> None:
        if not self.audit_id:
            self.audit_id = str(uuid.uuid4())

    def to_stream_message(self) -> dict[str, Any]:
        """转换为 trade_order Stream 消息格式。"""
        return {k: v for k, v in asdict(self).items() if v is not None}


# ─── 默认阈值 ─────────────────────────────────────

# AI 信号优先的置信度阈值（来自 ROLE_RISK.md）
_DEFAULT_AI_PRIORITY_CONFIDENCE = 0.8


# ─── 信号仲裁器 ─────────────────────────────────

class SignalArbiter:
    """
    AI 信号 vs Freqtrade 内置信号冲突仲裁器。

    用法:
        arbiter = SignalArbiter(circuit_breaker, position_sizer)
        order = await arbiter.arbitrate(
            ai_signal={"direction": "LONG", "confidence": 0.85, ...},
            freqtrade_signal={"action": "LONG", ...},
            ts=1700000000000,
        )
        # 如果仲裁通过，order.action == "LONG"
        # 如果熔断，order.action == "FLAT"
    """

    def __init__(
        self,
        circuit_breaker: CircuitBreaker | None = None,
        position_sizer: PositionSizer | None = None,
        drawdown_limit: DrawdownLimit | None = None,
        min_confidence: float | None = None,
        ai_priority_confidence: float = _DEFAULT_AI_PRIORITY_CONFIDENCE,
        require_regime_match: bool | None = None,
    ):
        """
        初始化信号仲裁器。

        参数:
            circuit_breaker: CircuitBreaker 实例（默认新建）
            position_sizer: PositionSizer 实例（默认新建）
            drawdown_limit: DrawdownLimit 实例（默认新建）
            min_confidence: AI 信号最低置信度（默认从 risk.yml 读取）
            ai_priority_confidence: AI 信号优先的置信度阈值（默认 0.8）
            require_regime_match: 是否要求制度匹配（默认从 risk.yml 读取）
        """
        self._breaker = circuit_breaker or CircuitBreaker()
        self._sizer = position_sizer or PositionSizer()
        self._drawdown = drawdown_limit or DrawdownLimit()

        signal_cfg = _load_signal_config()
        self._min_confidence = (
            float(min_confidence) if min_confidence is not None
            else signal_cfg["min_confidence"]
        )
        self._ai_priority_confidence = ai_priority_confidence
        self._require_regime_match = (
            bool(require_regime_match) if require_regime_match is not None
            else signal_cfg["require_regime_match"]
        )

        logger.info(
            "SignalArbiter 初始化",
            min_confidence=self._min_confidence,
            ai_priority_confidence=self._ai_priority_confidence,
            require_regime_match=self._require_regime_match,
        )

    def arbitrate(
        self,
        ai_signal: dict[str, Any] | None,
        freqtrade_signal: dict[str, Any] | None = None,
        ts: int | None = None,
        regime: str | None = None,
    ) -> ArbitratedOrder:
        """
        执行信号仲裁。

        参数:
            ai_signal: ai_signal Stream 消息（来自 AI 引擎）
                       包含 direction, confidence, entry, sl, tp, regime, reasoning
            freqtrade_signal: Freqtrade 内置信号（可选）
                              包含 action, entry, sl, tp
            ts: 毫秒时间戳，默认使用当前时间
            regime: 当前市场制度（用于制度匹配检查）

        返回:
            ArbitratedOrder — 仲裁后的最终交易指令
        """
        import time

        if ts is None:
            ts = int(time.time() * 1000)

        symbol = self._resolve_symbol(ai_signal, freqtrade_signal)

        # ─── 步骤 1: 熔断器检查 ──────────────────
        if not self._breaker.allow_open():
            logger.warning(
                "熔断器激活，拒绝新开仓",
                symbol=symbol,
                breaker_state=self._breaker.state.value,
            )
            return self._build_flat_order(
                symbol=symbol,
                ts=ts,
                source="ai_signal" if ai_signal else "freqtrade_native",
                reasoning=f"熔断器 {self._breaker.state.value}，拒绝新开仓",
            )

        # ─── 步骤 2: 回撤检查 ──────────────────
        if not self._drawdown.can_open_position():
            dd_status = self._drawdown.check_limits()
            logger.warning(
                "回撤限制中，拒绝新开仓",
                symbol=symbol,
                level=dd_status["level"],
            )
            return self._build_flat_order(
                symbol=symbol,
                ts=ts,
                source="ai_signal" if ai_signal else "freqtrade_native",
                reasoning=f"回撤限制 {dd_status['level']}，拒绝新开仓",
            )

        # ─── 步骤 3: 判断是否有 AI 信号 ──────────
        if ai_signal is None:
            return self._use_freqtrade_signal(
                freqtrade_signal=freqtrade_signal,
                symbol=symbol,
                ts=ts,
            )

        # ─── 步骤 4: 解析 AI 信号 ────────────────
        ai_direction = ai_signal.get("direction", "FLAT")
        ai_confidence = ai_signal.get("confidence", 0.0)
        ai_entry = ai_signal.get("entry")
        ai_sl = ai_signal.get("sl")
        ai_tp = ai_signal.get("tp")
        ai_regime = ai_signal.get("regime", "")
        ai_reasoning = ai_signal.get("reasoning", "")
        ai_score = ai_signal.get("score", 0.0)

        # ─── 步骤 5: 置信度阈值检查 ──────────────
        if ai_confidence < self._min_confidence:
            logger.info(
                "AI 信号置信度低于最低阈值，使用 Freqtrade 信号",
                symbol=symbol,
                ai_confidence=ai_confidence,
                min_confidence=self._min_confidence,
            )
            return self._use_freqtrade_signal(
                freqtrade_signal=freqtrade_signal,
                symbol=symbol,
                ts=ts,
            )

        # ─── 步骤 6: 制度匹配检查 ────────────────
        if self._require_regime_match and regime and ai_regime:
            if regime != ai_regime:
                logger.info(
                    "AI 信号制度不匹配，使用 Freqtrade 信号",
                    symbol=symbol,
                    current_regime=regime,
                    ai_regime=ai_regime,
                )
                return self._use_freqtrade_signal(
                    freqtrade_signal=freqtrade_signal,
                    symbol=symbol,
                    ts=ts,
                )

        # ─── 步骤 7: 仲裁核心 ──────────────────
        # ROLE_RISK.md 规则：AI 置信度 > 0.8 且 direction != FLAT → AI 信号优先
        if ai_direction != "FLAT" and ai_confidence >= self._ai_priority_confidence:
            return self._use_ai_signal(
                ai_signal=ai_signal,
                symbol=symbol,
                ts=ts,
                regime=regime or ai_regime,
            )

        # ─── 否则使用 Freqtrade 信号 ─────────────
        return self._use_freqtrade_signal(
            freqtrade_signal=freqtrade_signal,
            symbol=symbol,
            ts=ts,
        )

    # ─── 内部辅助 ────────────────────────────

    def _use_ai_signal(
        self,
        ai_signal: dict[str, Any],
        symbol: str,
        ts: int,
        regime: str,
    ) -> ArbitratedOrder:
        """使用 AI 信号生成交易指令。"""
        direction = ai_signal.get("direction", "FLAT")
        ai_entry = ai_signal.get("entry")
        ai_sl = ai_signal.get("sl")
        ai_tp = ai_signal.get("tp")
        ai_reasoning = ai_signal.get("reasoning", "")

        # 计算仓位
        if direction != "FLAT" and ai_entry:
            # 计算 Kelly 仓位（需要胜率和盈亏比，这里使用 AI signal 的 score 做近似）
            ai_score = ai_signal.get("score", 0.5)
            win_rate = max(0.1, min(0.9, ai_score))
            avg_rr = self._estimate_rr(ai_entry, ai_sl, ai_tp, direction)
            size_pct = self._sizer.calculate(
                win_rate=win_rate,
                avg_rr=avg_rr,
                regime=regime,
                equity=10000.0,  # 占位，实际由调用方提供
            )
        else:
            size_pct = 0.0

        order = ArbitratedOrder(
            symbol=symbol,
            ts=ts,
            action=direction,
            size_pct=size_pct,
            entry=ai_entry,
            sl=ai_sl,
            tp=ai_tp,
            source="ai_signal",
            breaker_state=self._breaker.state.value.upper(),
            reasoning=ai_reasoning,
        )

        logger.info(
            "仲裁结果：AI 信号优先",
            symbol=symbol,
            action=direction,
            size_pct=size_pct,
            confidence=ai_signal.get("confidence"),
            breaker_state=order.breaker_state,
            audit_id=order.audit_id,
        )

        return order

    def _use_freqtrade_signal(
        self,
        freqtrade_signal: dict[str, Any] | None,
        symbol: str,
        ts: int,
    ) -> ArbitratedOrder:
        """使用 Freqtrade 内置信号生成交易指令。"""
        if freqtrade_signal is None:
            return self._build_flat_order(
                symbol=symbol,
                ts=ts,
                source="freqtrade_native",
                reasoning="无 AI 信号且无 Freqtrade 信号",
            )

        action = freqtrade_signal.get("action", freqtrade_signal.get("direction", "FLAT"))
        entry = freqtrade_signal.get("entry", freqtrade_signal.get("entry_price"))
        sl = freqtrade_signal.get("sl", freqtrade_signal.get("stop_loss"))
        tp = freqtrade_signal.get("tp", freqtrade_signal.get("take_profit"))

        # Freqtrade 信号使用固定小仓位
        size_pct = 0.02  # 2%（保守）

        order = ArbitratedOrder(
            symbol=symbol,
            ts=ts,
            action=action,
            size_pct=size_pct,
            entry=entry,
            sl=sl,
            tp=tp,
            source="freqtrade_native",
            breaker_state=self._breaker.state.value.upper(),
            reasoning="Freqtrade 内置信号",
        )

        logger.info(
            "仲裁结果：Freqtrade 信号",
            symbol=symbol,
            action=action,
            size_pct=size_pct,
            breaker_state=order.breaker_state,
            audit_id=order.audit_id,
        )

        return order

    def _build_flat_order(
        self,
        symbol: str,
        ts: int,
        source: str,
        reasoning: str = "",
    ) -> ArbitratedOrder:
        """构建 FLAT（不开仓）指令。"""
        return ArbitratedOrder(
            symbol=symbol,
            ts=ts,
            action="FLAT",
            size_pct=0.0,
            source=source,
            breaker_state=self._breaker.state.value.upper(),
            reasoning=reasoning,
        )

    @staticmethod
    def _resolve_symbol(
        ai_signal: dict[str, Any] | None,
        freqtrade_signal: dict[str, Any] | None,
    ) -> str:
        """从任一信号中提取交易对。"""
        if ai_signal and ai_signal.get("symbol"):
            return str(ai_signal["symbol"])
        if freqtrade_signal and freqtrade_signal.get("symbol"):
            return str(freqtrade_signal["symbol"])
        if freqtrade_signal and freqtrade_signal.get("pair"):
            return str(freqtrade_signal["pair"])
        return "UNKNOWN"

    @staticmethod
    def _estimate_rr(
        entry: float | None,
        sl: float | None,
        tp: float | None,
        direction: str,
    ) -> float:
        """估算盈亏比。如果缺少价格数据，返回默认值 2.0。"""
        if not entry or not sl or not tp:
            return 2.0

        try:
            if direction == "LONG":
                profit = tp - entry
                loss = entry - sl
            elif direction == "SHORT":
                profit = entry - tp
                loss = sl - entry
            else:
                return 2.0

            if loss <= 0:
                return 2.0

            rr = profit / loss
            return max(0.5, min(rr, 10.0))  # 限制在合理范围
        except (TypeError, ZeroDivisionError):
            return 2.0

    def check_and_publish(
        self,
        ai_signal: dict[str, Any] | None,
        freqtrade_signal: dict[str, Any] | None = None,
        ts: int | None = None,
        regime: str | None = None,
    ) -> dict[str, Any]:
        """
        仲裁并返回 trade_order Stream 格式消息（可直接发布到 Redis Stream）。

        用法:
            producer = StreamProducer()
            order = arbiter.check_and_publish(ai_signal=signal)
            await producer.publish("trade_order", order)
        """
        order = self.arbitrate(
            ai_signal=ai_signal,
            freqtrade_signal=freqtrade_signal,
            ts=ts,
            regime=regime,
        )
        return order.to_stream_message()


__all__ = [
    "SignalArbiter",
    "ArbitratedOrder",
]

