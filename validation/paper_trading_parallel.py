"""
模块名称: paper_trading_parallel.py
所属层级: 验证层 (Validation)
输入来源: 回测信号列表（DataFrame）+ 模拟盘信号列表（DataFrame）
输出去向: ParallelComparisonReport（dataclass）
关键依赖: pandas · numpy

模拟盘与回测并行对比。
比较两个信号来源的方向一致性、置信度偏差、交易频次差异。
用于评估实盘与回测之间的信号漂移程度。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

# ─── 默认参数 ────────────────────────────────────

DEFAULT_MIN_SIGNALS: int = 10       # 最少信号数


# ─── 配置 ────────────────────────────────────────

@dataclass
class ParallelComparisonConfig:
    """并行对比配置。"""
    min_signals: int = DEFAULT_MIN_SIGNALS
    direction_col: str = "direction"
    confidence_col: str = "confidence"
    ts_col: str = "ts"


# ─── 报告模型 ────────────────────────────────────

@dataclass
class ParallelComparisonReport:
    """并行对比报告。"""
    total_signals: int
    matched_signals: int
    direction_agreement_pct: float    # 方向一致率 (0~1)
    confidence_correlation: float     # 置信度相关系数
    confidence_mean_diff: float       # 置信度均值差异
    signal_freq_ratio: float          # 信号频率比（模拟盘/回测）
    direction_bias: str               # "LONG" / "SHORT" / "NEUTRAL" — 哪个方向差异最大
    summary: str                      # 文本总结


# ─── 方向解析 ────────────────────────────────────

def _normalize_direction(d: Any) -> str:
    """将方向值归一化为大写字符串。"""
    if isinstance(d, str):
        return d.upper().strip()
    if d == 1:
        return "LONG"
    if d == -1:
        return "SHORT"
    return "FLAT"


# ─── 并行对比引擎 ────────────────────────────────

class PaperTradingParallel:
    """
    模拟盘与回测信号并行对比。

    用法:
        comparator = PaperTradingParallel()
        report = comparator.compare(backtest_signals, paper_signals)
        print(report.summary)
    """

    def __init__(self, config: ParallelComparisonConfig | None = None) -> None:
        self.config = config or ParallelComparisonConfig()

    def compare(
        self,
        backtest_signals: pd.DataFrame,
        paper_signals: pd.DataFrame,
    ) -> ParallelComparisonReport:
        """
        对比回测信号与模拟盘信号。

        参数:
            backtest_signals: 回测信号 DataFrame（ts, direction, confidence）
            paper_signals: 模拟盘信号 DataFrame（ts, direction, confidence）

        返回:
            ParallelComparisonReport
        """
        bt = backtest_signals.copy()
        pt = paper_signals.copy()

        if bt.empty or pt.empty:
            return ParallelComparisonReport(
                total_signals=0,
                matched_signals=0,
                direction_agreement_pct=0.0,
                confidence_correlation=0.0,
                confidence_mean_diff=0.0,
                signal_freq_ratio=0.0,
                direction_bias="NEUTRAL",
                summary="回测或模拟盘信号为空，无法对比",
            )

        # 标准化方向
        bt[self.config.direction_col] = bt[self.config.direction_col].apply(_normalize_direction)
        pt[self.config.direction_col] = pt[self.config.direction_col].apply(_normalize_direction)

        # 按时间戳对齐
        merged = pd.merge_asof(
            bt.sort_values(self.config.ts_col),
            pt.sort_values(self.config.ts_col),
            on=self.config.ts_col,
            direction="nearest",
            tolerance=3600000,  # 1h 内视为同一信号
            suffixes=("_bt", "_pt"),
        )
        merged = merged.dropna(subset=[f"{self.config.direction_col}_pt"])

        total_signals = len(merged)
        if total_signals < self.config.min_signals:
            return ParallelComparisonReport(
                total_signals=total_signals,
                matched_signals=0,
                direction_agreement_pct=0.0,
                confidence_correlation=0.0,
                confidence_mean_diff=0.0,
                signal_freq_ratio=float(len(pt)) / max(len(bt), 1),
                direction_bias="NEUTRAL",
                summary=f"对齐后仅 {total_signals} 个信号，不足最低 {self.config.min_signals}",
            )

        # 方向一致性
        dir_bt = merged[f"{self.config.direction_col}_bt"]
        dir_pt = merged[f"{self.config.direction_col}_pt"]
        matched = (dir_bt == dir_pt).sum()
        agreement = matched / total_signals

        # 方向偏差分析
        long_bt = (dir_bt == "LONG").sum()
        short_bt = (dir_bt == "SHORT").sum()
        long_pt = (dir_pt == "LONG").sum()
        short_pt = (dir_pt == "SHORT").sum()

        diff_long = abs(long_bt - long_pt)
        diff_short = abs(short_bt - short_pt)
        if diff_long > diff_short:
            direction_bias = "LONG"
        elif diff_short > diff_long:
            direction_bias = "SHORT"
        else:
            direction_bias = "NEUTRAL"

        # 置信度相关性
        conf_bt = merged[f"{self.config.confidence_col}_bt"]
        conf_pt = merged[f"{self.config.confidence_col}_pt"]

        conf_corr = conf_bt.corr(conf_pt)
        if np.isnan(conf_corr):
            conf_corr = 0.0

        conf_mean_diff = float((conf_bt - conf_pt).mean())

        # 信号频次比
        freq_ratio = float(len(pt)) / max(len(bt), 1)

        # 生成总结
        summary = (
            f"方向一致率: {agreement:.1%} ({matched}/{total_signals}); "
            f"置信度相关系数: {conf_corr:.3f}; "
            f"置信度偏差: {conf_mean_diff:+.3f}; "
            f"信号频次比: {freq_ratio:.2f}; "
            f"方向偏差: {direction_bias}"
        )

        logger.info(
            "并行对比完成",
            total_signals=total_signals,
            agreement=round(agreement, 3),
            conf_corr=round(conf_corr, 3),
        )

        return ParallelComparisonReport(
            total_signals=total_signals,
            matched_signals=int(matched),
            direction_agreement_pct=round(float(agreement), 4),
            confidence_correlation=round(float(conf_corr), 4),
            confidence_mean_diff=round(conf_mean_diff, 4),
            signal_freq_ratio=round(freq_ratio, 4),
            direction_bias=direction_bias,
            summary=summary,
        )


__all__ = [
    "ParallelComparisonConfig",
    "ParallelComparisonReport",
    "PaperTradingParallel",
]

