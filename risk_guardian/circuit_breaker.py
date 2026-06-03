"""
熔断器 — 风险控制核心
触发条件：
  1. 单日回撤超过 MAX_DAILY_DRAWDOWN_PCT
  2. 账户净值低于 EQUITY_FLOOR
  3. 连续亏损单数超过 MAX_CONSECUTIVE_LOSSES
熔断后：拒绝所有新开仓信号，只允许平仓
"""
import structlog
import os
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from risk_guardian.freqtrade_client import FreqtradeClient
from observability.alert_manager import alert_manager

logger = structlog.get_logger(__name__)


class BreakerState(str, Enum):
    CLOSED  = "closed"   # 正常
    OPEN    = "open"     # 熔断中
    HALF    = "half"     # 冷静期（仅允许小仓位）


@dataclass
class CircuitBreaker:
    max_daily_dd:     float = float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", 5.0))
    equity_floor:     float = float(os.getenv("EQUITY_FLOOR_USD", 0.0))
    max_consec_loss:  int   = int(os.getenv("MAX_CONSECUTIVE_LOSSES", 5))
    force_exit_enabled: bool = True
    state:            BreakerState = field(default=BreakerState.CLOSED, init=False)
    _consec:          int          = field(default=0, init=False)
    _day_start_eq:    float        = field(default=0.0, init=False)
    _today:           date         = field(default_factory=date.today, init=False)
    _freqtrade:       FreqtradeClient | None = field(default=None, init=False)

    def update_equity(self, current_equity: float) -> None:
        today = date.today()
        if today != self._today:
            self._day_start_eq = current_equity
            self._today = today
        if self._day_start_eq == 0:
            self._day_start_eq = current_equity
        # 先检查 equity floor（即使 _day_start_eq 刚初始化）
        if self.equity_floor and current_equity < self.equity_floor:
            self._trip(f"Equity {current_equity:.2f} below floor {self.equity_floor:.2f}")
            return
        if self._day_start_eq == 0:
            return
        dd = (self._day_start_eq - current_equity) / self._day_start_eq * 100
        if dd >= self.max_daily_dd:
            self._trip(f"Daily drawdown {dd:.2f}% >= {self.max_daily_dd}%")

    def record_trade(self, pnl: float) -> None:
        if pnl < 0:
            self._consec += 1
            if self._consec >= self.max_consec_loss:
                self._trip(f"Consecutive losses: {self._consec}")
        else:
            self._consec = 0

    def allow_open(self) -> bool:
        return self.state == BreakerState.CLOSED

    def reset(self) -> None:
        self.state = BreakerState.CLOSED
        logger.info("Circuit breaker reset")

    def _trip(self, reason: str) -> None:
        if self.state != BreakerState.OPEN:
            self.state = BreakerState.OPEN
            logger.critical("CIRCUIT BREAKER OPEN: %s", reason)
            try:
                import asyncio
                asyncio.ensure_future(alert_manager.critical(
                    "熔断器触发",
                    detail=reason,
                    tags={"breaker_state": "OPEN", "reason": reason},
                ))
            except Exception:
                pass
            self._force_exit_now(reason)

    def _force_exit_now(self, reason: str) -> None:
        """调用 Freqtrade REST API 强平所有持仓。"""
        if not self.force_exit_enabled:
            logger.info("force_exit 已禁用，跳过强平调用")
            return
        try:
            if self._freqtrade is None:
                self._freqtrade = FreqtradeClient()
            logger.warning("熔断器触发，调用 Freqtrade force_exit_all", reason=reason)
            result = self._freqtrade.force_exit_all()
            if result.success:
                logger.critical("强平成功：所有持仓已平仓", reason=reason, api_result=result.result[:100])
            else:
                logger.error("强平失败", reason=reason, error=result.error)
        except Exception as exc:
            logger.error("强平调用异常（忽略，不影响熔断状态）", reason=reason, error=str(exc))

    def set_freqtrade_client(self, client: FreqtradeClient | None) -> None:
        """设置自定义 FreqtradeClient（用于测试注入 mock）。"""
        self._freqtrade = client

