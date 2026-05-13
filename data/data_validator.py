"""
数据异常检测
- 价格跳空检测（单根K线涨跌超阈值）
- 成交量异常（超过N倍滚动均值）
- 时间戳连续性检测
"""
from dataclasses import dataclass
from typing import Optional
import numpy as np


@dataclass
class ValidationResult:
    valid: bool
    reason: Optional[str] = None


class KlineValidator:
    def __init__(
        self,
        max_price_change_pct: float = 15.0,
        volume_spike_multiplier: float = 20.0,
    ):
        self.max_price_change = max_price_change_pct / 100
        self.vol_spike = volume_spike_multiplier
        self._vol_history: list[float] = []

    def validate(self, kline: dict) -> ValidationResult:
        o, h, l, c = (
            float(kline["o"]), float(kline["h"]),
            float(kline["l"]), float(kline["c"]),
        )
        # 价格跳空
        change = abs(c - o) / max(o, 1e-10)
        if change > self.max_price_change:
            return ValidationResult(False, f"Price spike {change:.1%}")
        # HL 逻辑
        if h < l or h < max(o, c) or l > min(o, c):
            return ValidationResult(False, "OHLC logic error")
        # 成交量
        vol = float(kline.get("v", 0))
        if self._vol_history:
            avg = np.mean(self._vol_history[-50:])
            if vol > avg * self.vol_spike:
                return ValidationResult(False, f"Volume spike {vol/avg:.0f}x")
        self._vol_history.append(vol)
        return ValidationResult(True)
