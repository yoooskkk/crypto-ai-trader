"""
模块名称: pnl_attribution.py
所属层级: 分析层 (Analysis)
输入来源: decision_logger 日志 / 交易记录
输出去向: 因子贡献度分析报告（dict）
关键依赖: structlog, numpy

PnL 归因分析模块。
统计各因子对交易收益的贡献度。

修订记录:
- v1.0: 初始实现，多维度归因分析
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog

logger = structlog.get_logger(__name__)

# ─── 常量 ───────────────────────────────────────────────────────

DEFAULT_RISK_FREE_RATE = 0.02  # 年化无风险利率


# ─── 数据结构 ───────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    """单笔交易记录"""
    symbol: str
    direction: str  # LONG / SHORT
    entry_price: float
    exit_price: float
    volume: float
    entry_time: int  # Unix 毫秒
    exit_time: int
    pnl: float            # 实际盈亏
    pnl_pct: float        # 百分比盈亏
    confidence: float     # AI 置信度
    signal_score: float   # signal_scorer 评分
    regime: str           # 交易时市场制度
    factors: dict[str, float] = field(default_factory=dict)  # 交易时刻的因子值
    tag: str = ""         # 自定义标签，如 "LLM", "FALLBACK"


@dataclass
class AttributionReport:
    """归因分析报告"""
    total_pnl: float
    total_trades: int
    win_trades: int
    loss_trades: int
    win_rate: float
    sharpe: float
    sortino: float
    max_drawdown: float

    by_direction: dict[str, dict[str, Any]]   # LONG / SHORT 分项
    by_regime: dict[str, dict[str, Any]]      # 按制度分项
    by_tag: dict[str, dict[str, Any]]         # 按标签分项
    by_factor_corr: dict[str, float]          # 因子与 PnL 的相关系数
    top_factors: list[dict[str, Any]]         # 贡献度最高/最低的因子

    def summary_text(self) -> str:
        lines = [
            "=" * 50,
            "PnL Attribution Report",
            "=" * 50,
            f"Total PnL: {self.total_pnl:+.2f}",
            f"Trades: {self.total_trades} | Win: {self.win_rate:.1%}",
            f"Sharpe: {self.sharpe:.3f} | Sortino: {self.sortino:.3f}",
            f"Max Drawdown: {self.max_drawdown:.2%}",
            "",
            "--- Direction ---",
        ]
        for d, info in self.by_direction.items():
            lines.append(f"  {d}: {info['count']} trades, PnL={info['pnl']:+.2f}, Win={info['win_rate']:.1%}")
        lines.append("")
        lines.append("--- Regime ---")
        for r, info in self.by_regime.items():
            lines.append(f"  {r}: {info['count']} trades, PnL={info['pnl']:+.2f}")
        lines.append("")
        lines.append("--- Top Factors ---")
        for f in self.top_factors[:5]:
            lines.append(f"  {f['name']:16s}: corr={f['corr']:+.4f}")
        lines.append("=" * 50)
        return "\n".join(lines)


class PnLAttributor:
    """
    PnL 归因分析器。

    用法:
        attributor = PnLAttributor()
        report = attributor.analyze(trades)
        print(report.summary_text())
    """

    def analyze(self, trades: list[TradeRecord]) -> AttributionReport:
        """
        对交易记录进行归因分析。

        参数:
            trades: 交易记录列表

        返回:
            AttributionReport
        """
        if not trades:
            return self._empty_report()

        n = len(trades)
        pnls = np.array([t.pnl for t in trades])
        pnl_pcts = np.array([t.pnl_pct for t in trades])
        wins = pnls > 0

        # 基础统计
        total_pnl = float(np.sum(pnls))
        win_count = int(np.sum(wins))
        loss_count = n - win_count
        win_rate = win_count / n if n > 0 else 0.0

        # 夏普 / 索提诺
        sharpe = self._calc_sharpe(pnl_pcts)
        sortino = self._calc_sortino(pnl_pcts)

        # 最大回撤
        max_dd = self._calc_max_drawdown(pnl_pcts)

        # 按方向分组
        by_dir = self._group_by_key(trades, "direction")

        # 按制度分组
        by_reg = self._group_by_key(trades, "regime")

        # 按标签分组
        by_tag = self._group_by_key(trades, "tag")

        # 因子相关性
        factor_corr = self._calc_factor_correlations(trades)

        # Top 因子
        top = [
            {"name": k, "corr": round(v, 4)}
            for k, v in sorted(factor_corr.items(), key=lambda x: abs(x[1]), reverse=True)
        ]

        return AttributionReport(
            total_pnl=round(total_pnl, 4),
            total_trades=n,
            win_trades=win_count,
            loss_trades=loss_count,
            win_rate=round(win_rate, 4),
            sharpe=round(sharpe, 4),
            sortino=round(sortino, 4),
            max_drawdown=round(max_dd, 4),
            by_direction=by_dir,
            by_regime=by_reg,
            by_tag=by_tag,
            by_factor_corr={k: round(v, 4) for k, v in factor_corr.items()},
            top_factors=top[:10],
        )

    @staticmethod
    def _group_by_key(
        trades: list[TradeRecord], key: str,
    ) -> dict[str, dict[str, Any]]:
        """按维度分组统计"""
        groups: dict[str, list[float]] = {}
        for t in trades:
            k = getattr(t, key, "unknown")
            if k not in groups:
                groups[k] = []
            groups[k].append(t.pnl)

        result = {}
        for k, pnls_list in groups.items():
            arr = np.array(pnls_list)
            wins = int(np.sum(arr > 0))
            total = len(arr)
            result[k] = {
                "count": total,
                "pnl": round(float(np.sum(arr)), 4),
                "avg_pnl": round(float(np.mean(arr)), 4),
                "win_rate": round(wins / total, 4) if total > 0 else 0.0,
            }
        return result

    @staticmethod
    def _calc_sharpe(pnl_pcts: np.ndarray) -> float:
        """年化夏普比率"""
        if len(pnl_pcts) < 2:
            return 0.0
        excess = pnl_pcts - DEFAULT_RISK_FREE_RATE / 252
        std = float(np.std(excess))
        if std == 0:
            return 0.0
        return float(np.mean(excess)) / std * np.sqrt(252)

    @staticmethod
    def _calc_sortino(pnl_pcts: np.ndarray) -> float:
        """索提诺比率（只考虑下行波动）"""
        if len(pnl_pcts) < 2:
            return 0.0
        excess = pnl_pcts - DEFAULT_RISK_FREE_RATE / 252
        downside = excess[excess < 0]
        if len(downside) < 1:
            return float("inf") if np.mean(excess) > 0 else 0.0
        downside_std = float(np.std(downside))
        if downside_std == 0:
            return 0.0
        return float(np.mean(excess)) / downside_std * np.sqrt(252)

    @staticmethod
    def _calc_max_drawdown(pnl_pcts: np.ndarray) -> float:
        """最大回撤"""
        if len(pnl_pcts) < 2:
            return 0.0
        cumulative = np.cumsum(pnl_pcts)
        running_max = np.maximum.accumulate(cumulative)
        drawdown = cumulative - running_max
        return float(abs(np.min(drawdown)))

    @staticmethod
    def _calc_factor_correlations(
        trades: list[TradeRecord],
    ) -> dict[str, float]:
        """计算各因子值与 PnL 的相关系数"""
        if len(trades) < 5:
            return {}

        # 收集所有出现的因子
        all_factors: set[str] = set()
        for t in trades:
            all_factors.update(t.factors.keys())

        correlations: dict[str, float] = {}
        for factor in all_factors:
            values = []
            pnls = []
            for t in trades:
                if factor in t.factors:
                    v = t.factors[factor]
                    if v is not None and not (isinstance(v, float) and np.isnan(v)):
                        values.append(float(v))
                        pnls.append(float(t.pnl))
            if len(values) >= 30:  # 最小样本量
                try:
                    from scipy.stats import spearmanr
                    corr, _ = spearmanr(values, pnls)
                    if not np.isnan(corr):
                        correlations[factor] = float(corr)
                except Exception:
                    continue

        return correlations

    @staticmethod
    def _empty_report() -> AttributionReport:
        return AttributionReport(
            total_pnl=0.0,
            total_trades=0,
            win_trades=0,
            loss_trades=0,
            win_rate=0.0,
            sharpe=0.0,
            sortino=0.0,
            max_drawdown=0.0,
            by_direction={},
            by_regime={},
            by_tag={},
            by_factor_corr={},
            top_factors=[],
        )


__all__ = ["PnLAttributor", "TradeRecord", "AttributionReport"]

