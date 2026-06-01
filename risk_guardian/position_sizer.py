"""
模块名称: position_sizer.py
所属层级: 风险控制层 (Risk Guardian)
输入来源: 内部调用（由 signal_arbiter 调用）
输出去向: 返回值 float（仓位占总资产比例 size_pct）
关键依赖: config/risk.yml, risk_guardian/drawdown_limit.py

Kelly 公式仓位计算器。
  f* = (b * p - q) / b
  其中 b = 平均盈亏比，p = 胜率，q = 1-p

制度调整系数（来自 ARCH.md 第 7 节）：
  TRENDING        → 1.0，仓位上限 80%
  RANGING         → 0.5，仓位上限 40%
  HIGH_VOLATILITY → 0.5，仓位系数 ×0.5
  UNKNOWN         → 0.25，仓位上限 20%

Kelly 分数上限 25%（防止过激），再乘以制度系数。
最终仓位百分比受 risk.yml 中 max_total_pct 约束。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml

from risk_guardian.drawdown_limit import DrawdownLimit

logger = structlog.get_logger(__name__)


# ─── 配置路径 ────────────────────────────────────

_CONFIG_DIR = Path(__file__).parent.parent / "config"
RISK_CONFIG_PATH = _CONFIG_DIR / "risk.yml"


# ─── 制度调整系数（与 ARCH.md 第 7 节完全一致）───

REGIME_MULTIPLIER: dict[str, float] = {
    "TRENDING": 1.0,            # 仓位上限 80%
    "RANGING": 0.5,             # 仓位上限 40%（原 80% × 0.5）
    "HIGH_VOLATILITY": 0.5,     # 仓位系数 × 0.5
    "UNKNOWN": 0.25,            # 仓位上限 20%
}

_DEFAULT_REGIME = "UNKNOWN"
_DEFAULT_MULTIPLIER = 0.25

# Kelly 分数上限（防止过激）
MAX_KELLY_FRACTION = 0.25

# 最小仓位百分比（低于此值不下单）
MIN_POSITION_PCT = 0.01  # 1%


# ─── 辅助函数 ────────────────────────────────

def _load_exposure_limits(config_path: str | Path | None = None) -> dict[str, Any]:
    """从 risk.yml 加载持仓限制参数。"""
    cfg_path = Path(config_path) if config_path else RISK_CONFIG_PATH

    defaults = {
        "max_total_pct": 80.0,
        "max_single_position_pct": 20.0,
        "max_correlated_pairs": 3,
    }

    if not cfg_path.exists():
        logger.warning("risk.yml 未找到，使用默认持仓限制", path=str(cfg_path))
        return defaults

    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        exp_cfg = cfg.get("exposure", {})
        if not exp_cfg:
            return defaults

        return {
            "max_total_pct": float(exp_cfg.get("max_total_pct", defaults["max_total_pct"])),
            "max_single_position_pct": float(exp_cfg.get("max_single_position_pct", defaults["max_single_position_pct"])),
            "max_correlated_pairs": int(exp_cfg.get("max_correlated_pairs", defaults["max_correlated_pairs"])),
        }
    except Exception as exc:
        logger.error("加载持仓限制失败，使用默认值", error=str(exc))
        return defaults


def _load_signal_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """从 risk.yml 加载信号相关参数。"""
    cfg_path = Path(config_path) if config_path else RISK_CONFIG_PATH

    defaults = {
        "min_confidence": 0.65,
        "require_regime_match": True,
        "ai_override_freqtrade": False,
    }

    if not cfg_path.exists():
        return defaults

    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        sig_cfg = cfg.get("signal", {})
        return {
            "min_confidence": float(sig_cfg.get("min_confidence", defaults["min_confidence"])),
            "require_regime_match": bool(sig_cfg.get("require_regime_match", defaults["require_regime_match"])),
            "ai_override_freqtrade": bool(sig_cfg.get("ai_override_freqtrade", defaults["ai_override_freqtrade"])),
        }
    except Exception as exc:
        logger.error("加载信号参数失败，使用默认值", error=str(exc))
        return defaults


# ─── 仓位计算器 ─────────────────────────────────

class PositionSizer:
    """
    Kelly 公式仓位计算器。

    用法:
        sizer = PositionSizer()
        size_pct = sizer.calculate(
            win_rate=0.55,
            avg_rr=2.0,
            regime="TRENDING",
            equity=10000.0,
        )
        # 返回 0.08（8%）
    """

    def __init__(
        self,
        max_total_pct: float | None = None,
        max_single_pct: float | None = None,
        drawdown_tracker: DrawdownLimit | None = None,
    ):
        """
        初始化仓位计算器。

        参数:
            max_total_pct: 总仓位上限百分比（默认从 risk.yml 读取）
            max_single_pct: 单仓位上限百分比（默认从 risk.yml 读取）
            drawdown_tracker: DrawdownLimit 实例，用于回撤调整
        """
        limits = _load_exposure_limits()
        self._max_total_pct = (
            float(max_total_pct) if max_total_pct is not None
            else limits["max_total_pct"]
        )
        self._max_single_pct = (
            float(max_single_pct) if max_single_pct is not None
            else limits["max_single_position_pct"]
        )
        self._drawdown = drawdown_tracker or DrawdownLimit()

        logger.info(
            "PositionSizer 初始化",
            max_total_pct=self._max_total_pct,
            max_single_pct=self._max_single_pct,
        )

    def calculate(
        self,
        win_rate: float,
        avg_rr: float,
        regime: str,
        equity: float,
    ) -> float:
        """
        计算建议仓位百分比。

        参数:
            win_rate: 胜率（0.0~1.0）
            avg_rr: 平均盈亏比（例如 2.0 表示平均盈利/平均亏损 = 2）
            regime: 市场制度（TRENDING/RANGING/HIGH_VOLATILITY/UNKNOWN）
            equity: 当前总资产 USD

        返回:
            size_pct: 建议仓位占总资产的比例（0.0~1.0）
            返回 0.0 表示不应开仓。
        """
        # ─── 步骤 1: 检查回撤限制 ─────────────────
        if not self._drawdown.can_open_position():
            logger.warning(
                "回撤限制中，禁止开仓",
                level=self._drawdown.check_limits()["level"],
            )
            return 0.0

        dd_multiplier = self._drawdown.get_position_multiplier()
        if dd_multiplier <= 0.0:
            return 0.0

        # ─── 步骤 2: Kelly 公式 ──────────────────
        # f* = (b * p - q) / b
        # b = avg_rr, p = win_rate, q = 1 - p
        if avg_rr <= 0 or win_rate <= 0 or win_rate >= 1:
            logger.debug(
                "无效的参数，返回 0",
                win_rate=win_rate,
                avg_rr=avg_rr,
            )
            return 0.0

        q = 1.0 - win_rate
        kelly_fraction = (avg_rr * win_rate - q) / avg_rr

        # Kelly 分数上限 25%
        kelly_fraction = max(0.0, min(kelly_fraction, MAX_KELLY_FRACTION))

        # ─── 步骤 3: 制度调整 ──────────────────
        regime_mult = REGIME_MULTIPLIER.get(regime, _DEFAULT_MULTIPLIER)

        # ─── 步骤 4: 回撤调整 ──────────────────
        size_pct = kelly_fraction * regime_mult * dd_multiplier

        # ─── 步骤 5: 上限约束 ──────────────────
        max_pct_decimal = self._max_single_pct / 100.0
        size_pct = min(size_pct, max_pct_decimal)

        # ─── 步骤 6: 下限约束 ──────────────────
        if size_pct < MIN_POSITION_PCT:
            logger.debug(
                "仓位低于最低阈值，不开仓",
                size_pct=round(size_pct, 4),
                min_pct=MIN_POSITION_PCT,
            )
            return 0.0

        logger.info(
            "仓位计算完成",
            kelly_fraction=round(kelly_fraction, 4),
            regime_mult=regime_mult,
            dd_multiplier=dd_multiplier,
            size_pct=round(size_pct, 4),
            regime=regime,
        )

        return round(size_pct, 4)

    def calculate_size_value(
        self,
        win_rate: float,
        avg_rr: float,
        regime: str,
        equity: float,
    ) -> float:
        """
        计算建议仓位金额（USD）。

        参数:
            同 calculate()

        返回:
            建议开仓金额（USD）
        """
        size_pct = self.calculate(win_rate, avg_rr, regime, equity)
        return equity * size_pct

    @property
    def max_total_exposure_pct(self) -> float:
        """获取总仓位上限百分比。"""
        return self._max_total_pct

    @property
    def max_single_exposure_pct(self) -> float:
        """获取单仓位上限百分比。"""
        return self._max_single_pct


__all__ = [
    "PositionSizer",
    "REGIME_MULTIPLIER",
    "MAX_KELLY_FRACTION",
    "_load_exposure_limits",
    "_load_signal_config",
]

