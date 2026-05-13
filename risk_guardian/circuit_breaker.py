"""
熔断器 — 风险控制核心
触发条件：
  1. 单日回撤超过 MAX_DAILY_DRAWDOWN_PCT
  2. 账户净值低于 EQUITY_FLOOR
  3. 连续亏损单数超过 MAX_CONSECUTIVE_LOSSES
熔断后：拒绝所有新开仓信号，只允许平仓
"""
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

logger = logging.getLogger(__name__)


class BreakerState(str, Enum):
    CLOSED  = "closed"   # 正常
    OPEN    = "open"     # 熔断中
    HALF    = "half"     # 冷静期（仅允许小仓位）


@dataclass
class CircuitBreaker:
    max_daily_dd:   float = float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", 5.0))
    equity_floor:   float = float(os.getenv("EQUITY_FLOOR_USD", 0.0))
    max_consec_loss: int  = int(os.getenv("MAX_CONSECUTIVE_LOSSES", 5))
    state:          BreakerState = field(default=BreakerState.CLOSED, init=False)
    _consec:        int          = field(default=0, init=False)
    _day_start_eq:  float        = field(default=0.0, init=False)
    _today:         date         = field(default_factory=date.today, init=False)

    def update_equity(self, current_equity: float) -> None:
        today = date.today()
        if today != self._today:
            self._day_start_eq = current_equity
            self._today = today
        if self._day_start_eq == 0:
            self._day_start_eq = current_equity
            return
        dd = (self._day_start_eq - current_equity) / self._day_start_eq * 100
        if dd >= self.max_daily_dd:
            self._trip(f"Daily drawdown {dd:.2f}% >= {self.max_daily_dd}%")
        if self.equity_floor and current_equity < self.equity_floor:
            self._trip(f"Equity {current_equity:.2f} below floor {self.equity_floor:.2f}")

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
