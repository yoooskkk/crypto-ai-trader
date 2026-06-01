"""
模块名称: factor_mining.py
所属层级: 分析层 (Analysis)
输入来源: validation/datasets/train/（只读，铁律 #2）
输出去向: 结构化的因子排名列表（IC/IR 排序）
关键依赖: pandas, numpy, scipy, structlog

因子挖掘模块。
计算每个技术指标的 IC（Information Coefficient）和 IR（Information Ratio），
对因子进行排序和筛选。

【数据隔离 - 铁律 #2】
  - 只能读 validation/datasets/train/ 目录
  - 绝对不碰 validation/datasets/validate/
  - 绝对不碰 validation/datasets/oos/

修订记录:
- v1.0: 初始实现，IC/IR 计算 + 因子排名
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import structlog
from scipy.stats import spearmanr

logger = structlog.get_logger(__name__)

# ─── 常量 ───────────────────────────────────────────────────────

TRAIN_DATA_PATH = Path("validation/datasets/train/")
_VALIDATE_PATH = Path("validation/datasets/validate/")
_OOS_PATH = Path("validation/datasets/oos/")

_DEFAULT_FACTORS = [
    "SMA_20", "EMA_9", "EMA_21", "EMA_55",
    "RSI_14", "MACD_hist",
    "ATR_14", "BB_upper", "BB_lower", "BB_width",
    "OBV", "VWAP", "MFI_14", "CMF_20", "VOL_RATIO",
    "ADX_14", "STOCH_k", "STOCH_d", "ROC_10", "CCI_20",
]

_TREND_FACTORS = {"SMA_20", "EMA_9", "EMA_21", "EMA_55", "VWAP", "ADX_14"}
_MOMENTUM_FACTORS = {"RSI_14", "MACD_hist", "ROC_10", "CCI_20", "STOCH_k", "STOCH_d"}
_VOLATILITY_FACTORS = {"ATR_14", "BB_upper", "BB_lower", "BB_width"}
_VOLUME_FACTORS = {"OBV", "MFI_14", "CMF_20", "VOL_RATIO"}

MIN_TRAIN_SAMPLES = 30
MIN_IC_THRESHOLD = 0.02


# ─── 数据结构 ───────────────────────────────────────────────────────


@dataclass
class FactorResult:
    name: str
    category: str
    ic_mean: float
    ic_std: float
    ir: float
    ic_series: list[float] | None = None
    ic_win_rate: float = 0.0
    is_significant: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "ic_mean": round(self.ic_mean, 4),
            "ic_std": round(self.ic_std, 4),
            "ir": round(self.ir, 4),
            "ic_win_rate": round(self.ic_win_rate, 4),
            "is_significant": self.is_significant,
        }


class FactorMiner:
    """
    因子挖掘器。只读 validation/datasets/train/ 数据，计算各因子的 IC/IR。

    用法:
        miner = FactorMiner()
        results = miner.run()
        top = miner.get_top_factors(n=5)
        miner.log_report()
    """

    def __init__(
        self,
        train_path: str | Path | None = None,
        factors: list[str] | None = None,
        forward_periods: list[int] | None = None,
    ):
        raw_path = Path(train_path) if train_path else TRAIN_DATA_PATH
        self._validate_data_path(raw_path)
        self._train_path = raw_path
        self._factors = factors or _DEFAULT_FACTORS
        self._forward_periods = forward_periods or [1, 5]
        self._results: list[FactorResult] = []
        self._data: pd.DataFrame | None = None

    @staticmethod
    def _validate_data_path(path: Path) -> None:
        """铁律 #2：数据路径必须在 train/ 下"""
        resolved = path.resolve()
        for forbidden in [_VALIDATE_PATH, _OOS_PATH]:
            try:
                resolved.relative_to(forbidden.resolve())
                raise PermissionError(f"铁律 #2 违规：禁止访问 {forbidden}")
            except ValueError:
                pass
        try:
            resolved.relative_to(TRAIN_DATA_PATH.resolve())
        except ValueError:
            raise PermissionError(f"铁律 #2 违规：路径必须在 {TRAIN_DATA_PATH} 下")

    def load_data(self) -> pd.DataFrame | None:
        if self._data is not None:
            return self._data
        if not self._train_path.exists():
            logger.warning("目录不存在", path=str(self._train_path))
            return None
        for ext, reader in [
            (".parquet", pd.read_parquet),
            (".csv", pd.read_csv),
            (".feather", pd.read_feather),
        ]:
            fp = self._train_path / f"train{ext}"
            if fp.exists():
                try:
                    df = reader(fp)
                    self._data = df
                    return df
                except Exception as e:
                    logger.warning("加载失败", path=str(fp), error=str(e))
        candidates = list(self._train_path.glob("*.parquet")) + list(self._train_path.glob("*.csv"))
        if candidates:
            fp = candidates[0]
            reader = pd.read_parquet if fp.suffix == ".parquet" else pd.read_csv
            try:
                self._data = reader(fp)
                return self._data
            except Exception as e:
                logger.error("加载失败", path=str(fp), error=str(e))
        return None

    @staticmethod
    def compute_forward_returns(prices: pd.Series, periods: int = 1) -> pd.Series:
        return (prices.shift(-periods) - prices) / prices

    def compute_ic(self, factor: pd.Series, forward_ret: pd.Series) -> float | None:
        valid = factor.notna() & forward_ret.notna()
        if valid.sum() < MIN_TRAIN_SAMPLES:
            return None
        try:
            corr, _ = spearmanr(factor[valid].astype(float), forward_ret[valid].astype(float))
            return float(corr) if not np.isnan(corr) else None
        except Exception:
            return None

    @staticmethod
    def _categorize_factor(name: str) -> str:
        if name in _TREND_FACTORS:
            return "trend"
        elif name in _MOMENTUM_FACTORS:
            return "momentum"
        elif name in _VOLATILITY_FACTORS:
            return "volatility"
        elif name in _VOLUME_FACTORS:
            return "volume"
        return "unknown"

    def run(self, df: pd.DataFrame | None = None) -> list[FactorResult]:
        if df is None:
            df = self.load_data()
            if df is None:
                return []
        if "close" not in df.columns:
            for alt in ["Close", "close_price", "price"]:
                if alt in df.columns:
                    df = df.rename(columns={alt: "close"})
                    break
        if "close" not in df.columns:
            logger.error("数据缺少价格列")
            return []
        available = [f for f in self._factors if f in df.columns]
        if not available:
            return []
        results = []
        for name in available:
            ics = []
            for p in self._forward_periods:
                ic = self.compute_ic(df[name], self.compute_forward_returns(df["close"], p))
                if ic is not None:
                    ics.append(ic)
            if not ics:
                continue
            arr = np.array(ics)
            ic_m = float(np.mean(arr))
            ic_s = float(np.std(arr))
            results.append(FactorResult(
                name=name,
                category=self._categorize_factor(name),
                ic_mean=ic_m,
                ic_std=ic_s,
                ir=ic_m / ic_s if ic_s > 0 else 0.0,
                ic_series=ics if len(ics) > 1 else None,
                ic_win_rate=float(np.mean(arr > 0)),
                is_significant=abs(ic_m) > MIN_IC_THRESHOLD,
            ))
        results.sort(key=lambda r: r.ir, reverse=True)
        self._results = results
        logger.info("因子挖掘完成", total=len(results))
        return results

    def get_top_factors(self, n: int = 5, min_ir: float = 0.0, category: str | None = None) -> list[FactorResult]:
        candidates = self._results
        if category:
            candidates = [r for r in candidates if r.category == category]
        return [r for r in candidates if r.ir >= min_ir][:n]

    def get_category_summary(self) -> dict[str, dict[str, Any]]:
        s = {}
        for cat in ["trend", "momentum", "volatility", "volume", "unknown"]:
            items = [r for r in self._results if r.category == cat]
            if not items:
                continue
            s[cat] = {
                "count": len(items),
                "avg_ic": round(float(np.mean([r.ic_mean for r in items])), 4),
                "avg_ir": round(float(np.mean([r.ir for r in items])), 4),
                "best": max(items, key=lambda r: r.ir).name,
            }
        return s

    def log_report(self) -> None:
        if not self._results:
            return
        logger.info("=" * 50)
        logger.info("Factor Mining Report")
        logger.info("=" * 50)
        for i, r in enumerate(self._results, 1):
            flag = "*" if r.is_significant else " "
            logger.info(f"#{i:2d} [{flag}] {r.name:16s} IC={r.ic_mean:+.4f} IR={r.ir:+.4f} Win={r.ic_win_rate:.1%} [{r.category}]")
        logger.info("=" * 50)


__all__ = ["FactorMiner", "FactorResult", "TRAIN_DATA_PATH", "MIN_IC_THRESHOLD"]


