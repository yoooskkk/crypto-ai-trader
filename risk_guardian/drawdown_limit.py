"""
模块名称: drawdown_limit.py
所属层级: 风险控制层 (Risk Guardian)
输入来源: 内部调用（由 circuit_breaker 或 signal_arbiter 调用）
输出去向: 返回值 dict（是否允许开仓 / 是否需要强平）
关键依赖: config/risk.yml

最大回撤追踪，按日/周/月分级限制。
当回撤超过对应周期的阈值时，依次触发不同等级的保护：
  - DAILY 限制 → 当日禁止开新仓
  - WEEKLY 限制 → 降低仓位系数 ×0.5
  - MONTHLY 限制 → 强制平仓（通知 circuit_breaker）

所有阈值从 config/risk.yml 的 drawdown_limits 段读取。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger(__name__)

# ─── 配置路径 ────────────────────────────────────

_CONFIG_DIR = Path(__file__).parent.parent / "config"
RISK_CONFIG_PATH = _CONFIG_DIR / "risk.yml"


# ─── 回撤等级 ─────────────────────────────────────

class DrawdownLevel(str):
    """回撤等级常量。"""
    NORMAL = "NORMAL"         # 正常，未触发任何限制
    DAILY = "DAILY"           # 日回撤超限，禁止当日开新仓
    WEEKLY = "WEEKLY"         # 周回撤超限，仓位系数 ×0.5
    MONTHLY = "MONTHLY"       # 月回撤超限，需强制平仓


# ─── 默认阈值（当 risk.yml 缺少 drawdown_limits 段时使用）───

_DEFAULT_LIMITS: dict[str, float] = {
    "daily_max_pct": 5.0,      # 5%
    "weekly_max_pct": 10.0,    # 10%
    "monthly_max_pct": 15.0,   # 15%
    "recovery_half_days": 3,   # 回撤恢复后继续观察的天数
}


# ─── 辅助函数 ────────────────────────────────





def _load_drawdown_limits(config_path: str | Path | None = None) -> dict[str, Any]:
    """从 risk.yml 加载回撤限制参数。"""
    cfg_path = Path(config_path) if config_path else Path(RISK_CONFIG_PATH)

    if not cfg_path.exists():
        logger.warning("risk.yml 未找到，使用默认回撤参数", path=str(cfg_path))
        return dict(_DEFAULT_LIMITS)

    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        dd_cfg = cfg.get("drawdown_limits", {})
        if not dd_cfg:
            logger.info("risk.yml 无 drawdown_limits 段，使用默认参数")
            return dict(_DEFAULT_LIMITS)

        return {
            "daily_max_pct": float(dd_cfg.get("daily_max_pct", _DEFAULT_LIMITS["daily_max_pct"])),
            "weekly_max_pct": float(dd_cfg.get("weekly_max_pct", _DEFAULT_LIMITS["weekly_max_pct"])),
            "monthly_max_pct": float(dd_cfg.get("monthly_max_pct", _DEFAULT_LIMITS["monthly_max_pct"])),
            "recovery_half_days": int(dd_cfg.get("recovery_half_days", _DEFAULT_LIMITS["recovery_half_days"])),
        }
    except Exception as exc:
        logger.error("加载回撤参数失败，使用默认值", error=str(exc))
        return dict(_DEFAULT_LIMITS)


# ─── 回撤追踪器 ───────────────────────────────

@dataclass
class DrawdownLimit:
    """
    最大回撤追踪器，按日/周/月分级限制。

    用法:
        dd = DrawdownLimit()
        dd.update(peak_equity=10000.0, current_equity=9500.0)
        status = dd.check_limits()
        if status["level"] == "MONTHLY":
            # 触发强平
    """
    # 配置参数
    daily_max_pct: float = field(default=_DEFAULT_LIMITS["daily_max_pct"])
    weekly_max_pct: float = field(default=_DEFAULT_LIMITS["weekly_max_pct"])
    monthly_max_pct: float = field(default=_DEFAULT_LIMITS["monthly_max_pct"])
    recovery_half_days: int = field(default=_DEFAULT_LIMITS["recovery_half_days"])

    # 运行时状态
    peak_equity: float = field(default=0.0, init=False)       # 历史最高净值
    _day_start_eq: float = field(default=0.0, init=False)
    _week_start_eq: float = field(default=0.0, init=False)
    _month_start_eq: float = field(default=0.0, init=False)
    _today: date = field(default_factory=date.today, init=False)
    _week_start: date = field(default_factory=date.today, init=False)
    _month_start: date = field(default_factory=date.today, init=False)
    _recovery_date: date | None = field(default=None, init=False)

    # 当前回撤深度
    _current_drawdown_pct: float = field(default=0.0, init=False)

    def update(self, peak_equity: float, current_equity: float) -> None:
        """
        更新净值并重算回撤。

        参数:
            peak_equity: 历史最高净值（由外部传入，如 account equity curve）
            current_equity: 当前账户净值
        """
        today = date.today()

        # 初始化周期起始值（使用 peak_equity）
        if self._day_start_eq == 0.0:
            self._day_start_eq = peak_equity if peak_equity > 0 else current_equity
        if self._week_start_eq == 0.0:
            self._week_start_eq = peak_equity if peak_equity > 0 else current_equity
        if self._month_start_eq == 0.0:
            self._month_start_eq = peak_equity if peak_equity > 0 else current_equity

        # 检测日期变更 → 重置日/周/月起始值
        if today != self._today:
            self._day_start_eq = current_equity
            self._today = today

        # 周一起始
        if today.weekday() == 0 and today != self._week_start:
            self._week_start_eq = current_equity
            self._week_start = today

        # 月一起始
        if today.day == 1 and today != self._month_start:
            self._month_start_eq = current_equity
            self._month_start = today

        # 更新峰值
        self.peak_equity = max(self.peak_equity, peak_equity)

        # 计算当前回撤
        if self.peak_equity > 0:
            self._current_drawdown_pct = (
                (self.peak_equity - current_equity) / self.peak_equity * 100
            )
        else:
            self._current_drawdown_pct = 0.0

        # 恢复检测：如果当前净值已接近峰值（回撤恢复）
        if self._recovery_date is None and self._current_drawdown_pct < 2.0:
            # 回撤已基本恢复
            pass

        logger.debug(
            "回撤状态更新",
            peak_equity=round(peak_equity, 2),
            current_equity=round(current_equity, 2),
            drawdown_pct=round(self._current_drawdown_pct, 2),
        )

    def check_limits(self) -> dict[str, Any]:
        """
        检查当前回撤是否触发各周期限制。

        返回:
            {
                "level": "NORMAL" | "DAILY" | "WEEKLY" | "MONTHLY",
                "drawdown_pct": float,      # 当前回撤百分比
                "daily_dd": float,           # 日回撤
                "weekly_dd": float,          # 周回撤
                "monthly_dd": float,         # 月回撤
                "allow_new": bool,           # 是否允许开新仓
                "force_exit": bool,          # 是否需要强平
            }
        """
        daily_dd = self._calc_period_dd(self._day_start_eq)
        weekly_dd = self._calc_period_dd(self._week_start_eq)
        monthly_dd = self._calc_period_dd(self._month_start_eq)

        level = DrawdownLevel.NORMAL
        allow_new = True
        force_exit = False

        # 从高到低检查（月 > 周 > 日）
        if monthly_dd >= self.monthly_max_pct:
            level = DrawdownLevel.MONTHLY
            allow_new = False
            force_exit = True
        elif weekly_dd >= self.weekly_max_pct:
            level = DrawdownLevel.WEEKLY
            allow_new = False
            force_exit = False
        elif daily_dd >= self.daily_max_pct:
            level = DrawdownLevel.DAILY
            allow_new = False
            force_exit = False

        result = {
            "level": level,
            "drawdown_pct": round(self._current_drawdown_pct, 2),
            "daily_dd": round(daily_dd, 2),
            "weekly_dd": round(weekly_dd, 2),
            "monthly_dd": round(monthly_dd, 2),
            "allow_new": allow_new,
            "force_exit": force_exit,
        }

        if level != DrawdownLevel.NORMAL:
            logger.warning(
                "回撤限制触发",
                level=level,
                drawdown_pct=round(self._current_drawdown_pct, 2),
                daily_dd=round(daily_dd, 2),
                weekly_dd=round(weekly_dd, 2),
                monthly_dd=round(monthly_dd, 2),
            )

        return result

    def can_open_position(self) -> bool:
        """快捷方法：是否允许开新仓。"""
        return self.check_limits()["allow_new"]

    def get_position_multiplier(self) -> float:
        """
        根据当前回撤等级返回仓位乘数。

        返回:
            NORMAL → 1.0（正常）
            DAILY  → 0.0（禁止开仓）
            WEEKLY → 0.5（减半）
            MONTHLY → 0.0（禁止开仓+强平）
        """
        level = self.check_limits()["level"]
        if level in (DrawdownLevel.DAILY, DrawdownLevel.MONTHLY):
            return 0.0
        if level == DrawdownLevel.WEEKLY:
            return 0.5
        return 1.0

    def reset(self) -> None:
        """重置所有状态（例如新交易周期开始）。"""
        self.peak_equity = 0.0
        self._day_start_eq = 0.0
        self._week_start_eq = 0.0
        self._month_start_eq = 0.0
        self._current_drawdown_pct = 0.0
        self._recovery_date = None
        self._today = date.today()
        self._week_start = date.today()
        self._month_start = date.today()
        logger.info("回撤追踪器已重置")

    @staticmethod
    def from_config(config_path: str | Path | None = None) -> DrawdownLimit:
        """
        从 risk.yml 加载参数后创建实例。

        参数:
            config_path: risk.yml 路径，默认使用 config/risk.yml

        返回:
            DrawdownLimit 实例
        """
        params = _load_drawdown_limits(config_path)
        return DrawdownLimit(
            daily_max_pct=params["daily_max_pct"],
            weekly_max_pct=params["weekly_max_pct"],
            monthly_max_pct=params["monthly_max_pct"],
            recovery_half_days=params["recovery_half_days"],
        )

    # ─── 内部辅助 ────────────────────────────

    def _calc_period_dd(self, period_start_eq: float) -> float:
        """计算某个周期起始至今的回撤百分比。"""
        if period_start_eq <= 0:
            return 0.0
        # 用当前净值（从 peak 推算）计算该周期回撤
        current = self.peak_equity * (1 - self._current_drawdown_pct / 100)
        if current <= 0:
            return 0.0
        dd = (period_start_eq - current) / period_start_eq * 100
        return max(0.0, dd)


__all__ = [
    "DrawdownLevel",
    "DrawdownLimit",
    "_load_drawdown_limits",
]
