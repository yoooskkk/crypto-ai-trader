"""
市场制度识别
使用 ADX + BollingerBand 宽度的规则方法（快速）
或 HMM 模型（精确，需训练）
"""
from enum import Enum
from dataclasses import dataclass
import numpy as np


class Regime(str, Enum):
    TRENDING   = "TRENDING"
    RANGING    = "RANGING"
    HIGH_VOLAT = "HIGH_VOLATILITY"
    UNKNOWN    = "UNKNOWN"


@dataclass
class RegimeResult:
    regime: Regime
    confidence: float
    adx: float
    bb_width: float


class RuleBasedDetector:
    """
    规则:
      ADX > 25 且 BB宽度适中 → 趋势
      ADX < 20 且 BB宽度窄   → 震荡
      BB宽度极大             → 高波动
    """
    def __init__(self, adx_trend=25.0, adx_range=20.0, bb_wide=0.08, bb_narrow=0.02):
        self.adx_trend  = adx_trend
        self.adx_range  = adx_range
        self.bb_wide    = bb_wide
        self.bb_narrow  = bb_narrow

    def detect(self, adx: float, bb_width: float) -> RegimeResult:
        if bb_width > self.bb_wide:
            return RegimeResult(Regime.HIGH_VOLAT, 0.85, adx, bb_width)
        if adx > self.adx_trend:
            return RegimeResult(Regime.TRENDING, min(adx / 50, 1.0), adx, bb_width)
        if adx < self.adx_range and bb_width < self.bb_narrow:
            return RegimeResult(Regime.RANGING, 0.75, adx, bb_width)
        return RegimeResult(Regime.UNKNOWN, 0.4, adx, bb_width)
