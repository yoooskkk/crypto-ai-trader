"""
模块名称: factor_decay.py
所属层级: 验证层 (Validation)
输入来源: IC（信息系数）时序数据，由 analysis/factor_mining.py 计算
输出去向: FactorDecayReport（dataclass）
关键依赖: numpy · scipy.stats

因子衰减监控。
接收 IC 时序，计算移动平均、趋势斜率、半衰期，
在 IC 衰减至阈值以下或持续负向时发出告警。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog
from scipy import stats as sp_stats

logger = structlog.get_logger(__name__)

# ─── 默认参数 ────────────────────────────────────

DEFAULT_WINDOW: int = 20           # IC 移动窗口
DEFAULT_IC_THRESHOLD: float = 0.02  # IC 低于此值视为失效
DEFAULT_SLOPE_THRESHOLD: float = -0.001  # 斜率低于此值视为衰减
DEFAULT_HALF_LIFE_MAX: int = 60    # 半衰期超过此值视为正常（无衰减）


# ─── 配置 ────────────────────────────────────────

@dataclass
class FactorDecayConfig:
    """因子衰减监控配置。"""
    window: int = DEFAULT_WINDOW
    ic_threshold: float = DEFAULT_IC_THRESHOLD
    slope_threshold: float = DEFAULT_SLOPE_THRESHOLD
    half_life_max: int = DEFAULT_HALF_LIFE_MAX


# ─── 报告模型 ────────────────────────────────────

@dataclass
class FactorDecayReport:
    """因子衰减分析报告。"""
    factor_name: str
    ic_values: list[float]
    ic_mean: float
    ic_std: float
    ic_trend_slope: float
    ic_half_life: int
    is_decaying: bool
    alert_message: str = ""


# ─── 衰减监控器 ──────────────────────────────────

class FactorDecayMonitor:
    """
    因子衰减监控器。

    用法:
        monitor = FactorDecayMonitor(config=FactorDecayConfig())
        report = monitor.analyze(factor_name="momentum_1", ic_series=[0.05, 0.04, ...])
        if report.is_decaying:
            print(f"警告：{report.factor_name} 正在衰减")
    """

    def __init__(self, config: FactorDecayConfig | None = None) -> None:
        self.config = config or FactorDecayConfig()

    def analyze(
        self,
        factor_name: str,
        ic_series: list[float] | np.ndarray,
    ) -> FactorDecayReport:
        """
        分析因子 IC 衰减情况。

        参数:
            factor_name: 因子名称（如 "momentum_1"）
            ic_series: IC 时序值列表（按时间升序）

        返回:
            FactorDecayReport
        """
        ics = np.asarray(ic_series, dtype=float)

        if len(ics) < 2:
            return FactorDecayReport(
                factor_name=factor_name,
                ic_values=list(ics),
                ic_mean=float(np.mean(ics)) if len(ics) > 0 else 0.0,
                ic_std=0.0,
                ic_trend_slope=0.0,
                ic_half_life=self.config.half_life_max,
                is_decaying=False,
                alert_message="数据点不足，无法分析衰减",
            )

        # 移动平均 IC
        window = min(self.config.window, len(ics))
        ma_ic = self._moving_average(ics, window)

        # 整体 IC 均值 / 标准差
        ic_mean = float(np.mean(ics))
        ic_std = float(np.std(ics, ddof=1)) if len(ics) > 1 else 0.0

        # IC 趋势斜率（线性回归）
        slope = self._compute_trend_slope(ics)

        # IC 半衰期
        half_life = self._compute_half_life(ics)

        # 判断是否衰减
        is_decaying = False
        alerts: list[str] = []

        if ic_mean < self.config.ic_threshold:
            is_decaying = True
            alerts.append(
                f"IC 均值 {ic_mean:.4f} 低于阈值 {self.config.ic_threshold}"
            )

        if slope < self.config.slope_threshold:
            is_decaying = True
            alerts.append(
                f"IC 斜率 {slope:.6f} 低于阈值 {self.config.slope_threshold}，趋势衰减"
            )

            # 半衰期短 = 衰减速度快
            # _compute_half_life 返回 max_periods 表示测量窗口内未检测到衰减
            # 此时不视为衰减
            max_checked = min(len(ics) - 1, 60)  # 与 _compute_half_life 内部的 max_periods 一致
            if half_life < max_checked:
                is_decaying = True
                alerts.append(
                    f"IC 半衰期仅 {half_life} 期（< {max_checked}），衰减过快"
                )

        if is_decaying:
            logger.warning(
                "因子衰减检测到",
                factor=factor_name,
                ic_mean=round(ic_mean, 4),
                slope=round(slope, 6),
                half_life=half_life,
            )

        return FactorDecayReport(
            factor_name=factor_name,
            ic_values=list(ics),
            ic_mean=round(ic_mean, 4),
            ic_std=round(ic_std, 4),
            ic_trend_slope=round(slope, 6),
            ic_half_life=half_life,
            is_decaying=is_decaying,
            alert_message="; ".join(alerts),
        )

    # ── 内部方法 ───────────────────────────────

    @staticmethod
    def _moving_average(arr: np.ndarray, window: int) -> float:
        """计算最近 window 期的移动平均。"""
        if len(arr) < window:
            window = len(arr)
        return float(np.mean(arr[-window:]))

    @staticmethod
    def _compute_trend_slope(arr: np.ndarray) -> float:
        """
        用线性回归计算 IC 时序的斜率。
        负斜率 = IC 随时间下降（衰减）。
        """
        x = np.arange(len(arr))
        try:
            slope, _intercept, _r_value, _p_value, _std_err = sp_stats.linregress(x, arr)
            return slope
        except Exception:
            return 0.0

    @staticmethod
    def _compute_half_life(arr: np.ndarray) -> int:
        """
        计算 IC 的半衰期：自相关系数衰减至 0.5 以下所需期数。
        如果始终未衰减至 0.5 以下，返回 max_periods。
        """
        n = len(arr)
        max_periods = min(n - 1, 60)

        for lag in range(1, max_periods + 1):
            x = arr[:-lag]
            y = arr[lag:]
            if len(x) < 2:
                continue
            try:
                corr, _ = sp_stats.pearsonr(x, y)
                if corr < 0.5:
                    return lag
            except Exception:
                continue

        return max_periods


__all__ = [
    "FactorDecayConfig",
    "FactorDecayReport",
    "FactorDecayMonitor",
]

