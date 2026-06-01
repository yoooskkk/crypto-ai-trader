"""
模块名称: oos_test.py
所属层级: 验证层 (Validation)
输入来源: OOS 数据集（validation/datasets/oos/）+ 信号 DataFrame
输出去向: OOSTestReport（dataclass）
关键依赖: validation/output_schema.py · validation/walk_forward.py

OOS（Out-of-Sample）封存测试。
铁律 #3：OOS 数据只在上线前评估用一次，使用后标记为已用。
通过标记文件（.oos_used）防止重复使用。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
import structlog

from validation.output_schema import BacktestResult
from validation.walk_forward import _run_window_backtest, _compute_metrics

logger = structlog.get_logger(__name__)

# ─── OOS 路径常量 ────────────────────────────────

OOS_DATA_DIR = Path(__file__).parent / "datasets" / "oos"
OOS_LOCK_FILE = OOS_DATA_DIR / ".oos_used"


# ─── 配置 ────────────────────────────────────────

@dataclass
class OOSTestConfig:
    """OOS 测试配置。"""
    data_dir: Path = OOS_DATA_DIR
    lock_file: Path = OOS_LOCK_FILE
    min_trades: int = 5
    enforce_single_use: bool = True   # 铁律 #3：强制单次使用
    price_col: str = "close"
    ts_col: str = "ts"


# ─── 报告模型 ────────────────────────────────────

@dataclass
class OOSTestReport:
    """OOS 测试报告。"""
    tested_at: str
    symbol: str
    total_trades: int
    win_rate: float
    sharpe: float
    max_drawdown: float
    profit_factor: float
    avg_trade_pct: float
    is_valid: bool
    was_already_used: bool
    message: str = ""


# ─── OOS 测试引擎 ────────────────────────────────

class OOSTestEngine:
    """
    OOS 封存测试引擎。

    用法:
        engine = OOSTestEngine()
        report = engine.run(prices_df, signals_df, symbol="BTCUSDT")
        if report.is_valid:
            print(f"OOS 通过：夏普 {report.sharpe}")
    """

    def __init__(self, config: OOSTestConfig | None = None) -> None:
        self.config = config or OOSTestConfig()

    def run(
        self,
        prices: pd.DataFrame,
        signals: pd.DataFrame,
        symbol: str = "",
    ) -> OOSTestReport:
        """
        执行 OOS 测试。

        参数:
            prices: OOS 期价格数据
            signals: OOS 期信号数据
            symbol: 交易对名称

        返回:
            OOSTestReport
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 检查是否已被使用
        already_used = self._is_already_used()

        if already_used and self.config.enforce_single_use:
            logger.warning("OOS 数据已被使用过，禁止重复使用（铁律 #3）")
            return OOSTestReport(
                tested_at=timestamp,
                symbol=symbol,
                total_trades=0,
                win_rate=0.0,
                sharpe=0.0,
                max_drawdown=0.0,
                profit_factor=0.0,
                avg_trade_pct=0.0,
                is_valid=False,
                was_already_used=True,
                message="OOS 数据已被使用过，禁止重复使用（铁律 #3）",
            )

        # 数据检查
        if prices.empty or signals.empty:
            return OOSTestReport(
                tested_at=timestamp,
                symbol=symbol,
                total_trades=0,
                win_rate=0.0,
                sharpe=0.0,
                max_drawdown=0.0,
                profit_factor=0.0,
                avg_trade_pct=0.0,
                is_valid=False,
                was_already_used=False,
                message="价格或信号数据为空",
            )

        # 执行回测
        bt_result = _run_window_backtest(
            prices, signals,
            ts_col=self.config.ts_col,
            price_col=self.config.price_col,
        )

        if bt_result.trades < self.config.min_trades:
            logger.warning(
                "OOS 交易次数不足",
                trades=bt_result.trades,
                min_required=self.config.min_trades,
            )
            metrics = _compute_metrics(bt_result)
            return OOSTestReport(
                tested_at=timestamp,
                symbol=symbol,
                total_trades=bt_result.trades,
                win_rate=metrics.win_rate if metrics else 0.0,
                sharpe=metrics.sharpe if metrics else 0.0,
                max_drawdown=metrics.max_drawdown if metrics else 0.0,
                profit_factor=metrics.profit_factor if metrics else 0.0,
                avg_trade_pct=metrics.avg_trade_pct if metrics else 0.0,
                is_valid=False,
                was_already_used=False,
                message=f"交易次数 {bt_result.trades} 低于最小要求 {self.config.min_trades}",
            )

        metrics = _compute_metrics(bt_result)
        if metrics is None:
            return OOSTestReport(
                tested_at=timestamp,
                symbol=symbol,
                total_trades=bt_result.trades,
                win_rate=0.0,
                sharpe=0.0,
                max_drawdown=0.0,
                profit_factor=0.0,
                avg_trade_pct=0.0,
                is_valid=False,
                was_already_used=False,
                message="指标计算失败",
            )

        # 标记 OOS 已使用（铁律 #3）
        self._mark_used()

        logger.info(
            "OOS 测试完成",
            symbol=symbol,
            trades=metrics.total_trades,
            sharpe=metrics.sharpe,
            win_rate=metrics.win_rate,
        )

        return OOSTestReport(
            tested_at=timestamp,
            symbol=symbol,
            total_trades=metrics.total_trades,
            win_rate=metrics.win_rate,
            sharpe=metrics.sharpe,
            max_drawdown=metrics.max_drawdown,
            profit_factor=metrics.profit_factor,
            avg_trade_pct=metrics.avg_trade_pct,
            is_valid=True,
            was_already_used=False,
            message="OOS 测试通过",
        )

    # ── OOS 使用状态管理 ───────────────────────

    def _is_already_used(self) -> bool:
        """检查 OOS 是否已被使用过。"""
        return self.config.lock_file.exists()

    def _mark_used(self) -> None:
        """标记 OOS 为已使用。"""
        try:
            self.config.data_dir.mkdir(parents=True, exist_ok=True)
            self.config.lock_file.write_text(
                f"OOS used at {datetime.now().isoformat()}\n"
            )
        except OSError as exc:
            logger.error("无法写入 OOS 标记文件", error=str(exc))

    def reset_lock(self) -> bool:
        """重置 OOS 使用标记（仅用于测试场景）。"""
        try:
            if self.config.lock_file.exists():
                self.config.lock_file.unlink()
                logger.info("OOS 标记已重置")
                return True
            return False
        except OSError as exc:
            logger.error("重置 OOS 标记失败", error=str(exc))
            return False


__all__ = [
    "OOSTestConfig",
    "OOSTestReport",
    "OOSTestEngine",
    "OOS_DATA_DIR",
    "OOS_LOCK_FILE",
]

