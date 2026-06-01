"""
模块名称: walk_forward.py
所属层级: 验证层 (Validation)
输入来源: OHLCV DataFrame（外部传入） + 信号 DataFrame（由回测框架或 ai_signal 历史数据提供）
输出去向: WalkForwardResult（output_schema.py 定义的 Pydantic 模型）
关键依赖: validation/output_schema.py · numpy · pandas

滚动窗口验证框架。
将历史数据按「训练窗口 → 验证窗口」方式分段，在每个窗口上执行简化的回测，
收集 BacktestResult 并汇总为 WalkForwardResult，评估策略的稳健性。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
import structlog

from validation.output_schema import BacktestResult, WalkForwardResult

logger = structlog.get_logger(__name__)

# ─── 默认配置参数 ────────────────────────────────

DEFAULT_TRAIN_DAYS: int = 90          # 训练窗口长度（天）
DEFAULT_VALIDATE_DAYS: int = 30       # 验证窗口长度（天）
DEFAULT_STEP_DAYS: int = 30           # 步进长度（天）
DEFAULT_MIN_TRADES: int = 5           # 一个窗口最少交易次数，低于此视为无效
DEFAULT_ROBUST_THRESHOLD: float = 2.0  # sharpe_variance 低于此值视为稳健
RISK_FREE_RATE: float = 0.02          # 无风险利率（用于夏普计算）


# ─── 配置 ────────────────────────────────────────

@dataclass
class WalkForwardConfig:
    """Walk-Forward 验证配置。"""
    train_days: int = DEFAULT_TRAIN_DAYS
    validate_days: int = DEFAULT_VALIDATE_DAYS
    step_days: int = DEFAULT_STEP_DAYS
    min_trades: int = DEFAULT_MIN_TRADES
    robust_threshold: float = DEFAULT_ROBUST_THRESHOLD


# ─── Signal 方向映射 ─────────────────────────────

_SIGNAL_LONG = 1
_SIGNAL_SHORT = -1
_SIGNAL_FLAT = 0


def _parse_signal_direction(direction: str) -> int:
    """将字符串方向转为数值方向。"""
    d = direction.upper().strip()
    if d == "LONG":
        return _SIGNAL_LONG
    if d == "SHORT":
        return _SIGNAL_SHORT
    return _SIGNAL_FLAT


# ─── 窗口回测引擎（简化版）────────────────────────

@dataclass
class _WindowBacktestResult:
    """单个窗口的回测中间结果。"""
    trades: int = 0
    wins: int = 0
    returns: list[float] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)


def _run_window_backtest(
    prices: pd.DataFrame,
    signals: pd.DataFrame,
    ts_col: str,
    price_col: str = "close",
) -> _WindowBacktestResult:
    """
    在单个窗口上执行简化回测。

    参数:
        prices: 价格 DataFrame（必须包含 ts_col 和 price_col）
        signals: 信号 DataFrame（必须包含 ts_col + direction 字段），
                 按时间升序排列
        ts_col: 时间戳列名（毫秒级 int，或 datetime 字符串）
        price_col: 价格列名，默认 "close"

    返回:
        _WindowBacktestResult
    """
    result = _WindowBacktestResult(equity_curve=[1.0])

    if prices.empty or signals.empty:
        return result

    # 合并价格与信号
    merged = pd.merge_asof(
        signals.sort_values(ts_col),
        prices[[ts_col, price_col]].sort_values(ts_col),
        on=ts_col,
        direction="forward",
    )
    merged = merged.dropna(subset=[price_col]).reset_index(drop=True)

    if merged.empty:
        return result

    position = _SIGNAL_FLAT
    entry_price = 0.0
    equity = 1.0

    for _, row in merged.iterrows():
        signal_dir = _parse_signal_direction(row.get("direction", "FLAT"))
        current_price = float(row[price_col])

        # 平仓（方向变 FLAT 或反向）
        if position != _SIGNAL_FLAT and signal_dir != position:
            ret = (current_price / entry_price - 1.0) * position
            result.returns.append(ret)
            result.trades += 1
            if ret > 0:
                result.wins += 1
            equity *= (1.0 + ret)
            result.equity_curve.append(equity)
            position = _SIGNAL_FLAT

        # 开仓
        if signal_dir != _SIGNAL_FLAT and position == _SIGNAL_FLAT:
            position = signal_dir
            entry_price = current_price

    # 窗口结束时强平
    if position != _SIGNAL_FLAT and len(merged) > 0:
        last_price = float(merged[price_col].iloc[-1])
        ret = (last_price / entry_price - 1.0) * position
        result.returns.append(ret)
        result.trades += 1
        if ret > 0:
            result.wins += 1
        equity *= (1.0 + ret)
        result.equity_curve.append(equity)

    return result


def _compute_metrics(result: _WindowBacktestResult) -> BacktestResult | None:
    """
    将窗口中间结果转为 BacktestResult。

    返回:
        若交易次数 >= 1 则返回 BacktestResult，否则返回 None
    """
    if result.trades < 1:
        return None

    returns = np.array(result.returns)
    win_rate = result.wins / result.trades if result.trades > 0 else 0.0
    avg_trade_pct = float(np.mean(returns)) * 100.0

    # 夏普比率（年化）
    excess = returns - RISK_FREE_RATE / 365.0
    sharpe = 0.0
    if np.std(excess, ddof=1) > 1e-10:
        sharpe = float(np.mean(excess) / np.std(excess, ddof=1) * np.sqrt(365))

    # 最大回撤
    equity = np.array(result.equity_curve)
    max_dd = 0.0
    if len(equity) > 1:
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / peak
        max_dd = float(np.min(dd))

    # 盈亏比
    profit_factor = 0.0
    gross_profit = sum(r for r in returns if r > 0)
    gross_loss = abs(sum(r for r in returns if r < 0))
    if gross_loss > 1e-10:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = float("inf")

    return BacktestResult(
        total_trades=result.trades,
        win_rate=round(win_rate, 4),
        sharpe=round(sharpe, 4),
        max_drawdown=round(max_dd, 4),
        profit_factor=round(profit_factor, 4) if profit_factor != float("inf") else 9999.0,
        avg_trade_pct=round(avg_trade_pct, 4),
        strategy="",
        start_date="",
        end_date="",
    )


# ─── Walk-Forward 主引擎 ────────────────────────

class WalkForwardEngine:
    """
    滚动窗口验证引擎。

    用法:
        engine = WalkForwardEngine(config=WalkForwardConfig(...))
        result = engine.run(prices_df, signals_df)
        print(result.avg_sharpe, result.robust)
    """

    def __init__(self, config: WalkForwardConfig | None = None) -> None:
        self.config = config or WalkForwardConfig()

    # ── 公开接口 ─────────────────────────────

    def run(
        self,
        prices: pd.DataFrame,
        signals: pd.DataFrame,
        ts_col: str = "ts",
        price_col: str = "close",
        symbol: str = "",
    ) -> WalkForwardResult:
        """
        执行 Walk-Forward 验证。

        参数:
            prices: OHLCV 价格 DataFrame，必须包含 ts_col + price_col
            signals: 信号 DataFrame，必须包含 ts_col + direction 字段
            ts_col: 时间戳列名
            price_col: 价格列名
            symbol: 交易对名称（可选，用于 BacktestResult.strategy）

        返回:
            WalkForwardResult（含所有窗口的 BacktestResult 列表 + 汇总统计）
        """
        prices = prices.copy()
        signals = signals.copy()

        # 确保时间戳排序
        prices = prices.sort_values(ts_col).reset_index(drop=True)
        signals = signals.sort_values(ts_col).reset_index(drop=True)

        if prices.empty or signals.empty:
            logger.warning("价格或信号数据为空，返回空结果")
            return WalkForwardResult(
                windows=[],
                avg_sharpe=0.0,
                sharpe_variance=0.0,
                robust=False,
            )

        # 解析时间范围
        min_ts = max(
            int(prices[ts_col].min()),
            int(signals[ts_col].min()),
        )
        max_ts = min(
            int(prices[ts_col].max()),
            int(signals[ts_col].max()),
        )

        if max_ts <= min_ts:
            logger.warning("数据时间范围不足，返回空结果")
            return WalkForwardResult(
                windows=[],
                avg_sharpe=0.0,
                sharpe_variance=0.0,
                robust=False,
            )

        # 生成滚动窗口
        windows = self._generate_windows(min_ts, max_ts)
        if not windows:
            logger.warning(
                "生成窗口数为 0，数据可能太短",
                days=(max_ts - min_ts) / 86400000,
            )
            return WalkForwardResult(
                windows=[],
                avg_sharpe=0.0,
                sharpe_variance=0.0,
                robust=False,
            )

        logger.info(
            "Walk-Forward 开始",
            windows=len(windows),
            symbol=symbol,
            date_range=f"{_ts_to_date(min_ts)} ~ {_ts_to_date(max_ts)}",
        )

        # 逐窗口回测
        backtest_results: list[BacktestResult] = []
        for i, (train_start, train_end, val_start, val_end) in enumerate(windows):
            # 筛选验证期信号
            val_signals = signals[
                (signals[ts_col] >= val_start) & (signals[ts_col] <= val_end)
            ].copy()

            val_prices = prices[
                (prices[ts_col] >= val_start) & (prices[ts_col] <= val_end)
            ].copy()

            if val_signals.empty or val_prices.empty:
                continue

            bt_result = _run_window_backtest(val_prices, val_signals, ts_col, price_col)

            # 检查最少交易次数
            if bt_result.trades < self.config.min_trades:
                logger.debug(
                    "窗口跳过：交易次数不足",
                    window=i,
                    trades=bt_result.trades,
                    min_required=self.config.min_trades,
                )
                continue

            metrics = _compute_metrics(bt_result)
            if metrics is not None:
                metrics.strategy = symbol if symbol else f"window_{i}"
                metrics.start_date = _ts_to_date(val_start)
                metrics.end_date = _ts_to_date(val_end)
                backtest_results.append(metrics)

        # 汇总
        return self._aggregate(backtest_results)

    # ── 窗口生成 ─────────────────────────────

    def _generate_windows(
        self,
        min_ts: int,
        max_ts: int,
    ) -> list[tuple[int, int, int, int]]:
        """
        生成滚动窗口列表。

        每个窗口: (train_start, train_end, val_start, val_end)
        时间戳为毫秒级 int。
        """
        train_ms = self.config.train_days * 86400000
        val_ms = self.config.validate_days * 86400000
        step_ms = self.config.step_days * 86400000

        windows: list[tuple[int, int, int, int]] = []
        cursor = min_ts

        while cursor + train_ms + val_ms <= max_ts:
            train_start = cursor
            train_end = cursor + train_ms
            val_start = train_end
            val_end = val_start + val_ms
            windows.append((train_start, train_end, val_start, val_end))
            cursor += step_ms

        return windows

    # ── 汇总 ─────────────────────────────────

    @staticmethod
    def _aggregate(results: list[BacktestResult]) -> WalkForwardResult:
        """汇总所有窗口结果。"""
        if not results:
            return WalkForwardResult(
                windows=[],
                avg_sharpe=0.0,
                sharpe_variance=0.0,
                robust=False,
            )

        sharpes = np.array([r.sharpe for r in results])
        avg_sharpe = float(np.mean(sharpes))
        sharpe_variance = float(np.var(sharpes, ddof=1)) if len(sharpes) > 1 else 0.0
        robust = sharpe_variance < DEFAULT_ROBUST_THRESHOLD

        return WalkForwardResult(
            windows=results,
            avg_sharpe=round(avg_sharpe, 4),
            sharpe_variance=round(sharpe_variance, 4),
            robust=robust,
        )


# ─── 辅助函数 ───────────────────────────────────

def _ts_to_date(ts: int) -> str:
    """将毫秒时间戳转为 'YYYY-MM-DD' 格式。"""
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _date_str_to_ts(date_str: str) -> int:
    """将 'YYYY-MM-DD' 格式日期转为毫秒时间戳。"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return int(dt.timestamp() * 1000)


__all__ = [
    "WalkForwardConfig",
    "WalkForwardEngine",
    "WalkForwardResult",
    "_run_window_backtest",
    "_compute_metrics",
]
