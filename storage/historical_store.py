"""
模块名称: historical_store.py
所属层级: 数据存储层 (Storage)
输入来源: Binance WebSocket / 回填脚本
输出去向: Parquet 文件（历史 K 线）+ SQLite（决策日志）
关键依赖: pandas, pyarrow

Parquet 格式存储历史 K 线数据。
替代 TimescaleDB klines 超表，零守护进程，压缩比 10:1。

存储结构:
    data/historical/
    ├── klines/
    │   ├── BTCUSDT_1h.parquet      ← 主文件（每根 K 线一行）
    │   ├── BTCUSDT_4h.parquet
    │   └── ETHUSDT_1h.parquet
    ├── indicators/                   ← 指标历史（可选）
    └── decisions.db                 ← SQLite 决策日志

优势:
    - 无数据库守护进程（省 ~800MB RAM）
    - Parquet 列式存储，查询效率高
    - 文件级备份，复制即用
    - 支持 pandas 直接读取分析
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

# ─── 默认路径 ─────────────────────────────────────

_DEFAULT_DATA_DIR = os.getenv("HISTORICAL_DATA_PATH", "data/historical")


# ─── 历史 K 线存储 ─────────────────────────────────

class KlineStore:
    """
    基于 Parquet 的历史 K 线存储。

    用法:
        store = KlineStore()
        df = pd.DataFrame({...})  # 包含 open/high/low/close/volume
        store.append("BTCUSDT", "1h", df)
        df = store.query("BTCUSDT", "1h", limit=500)
    """

    # Parquet 列定义
    COLUMNS = ["ts", "open", "high", "low", "close", "volume"]
    DTYPES: dict[str, type] = {
        "ts": "int64[pyarrow]",
        "open": "float64[pyarrow]",
        "high": "float64[pyarrow]",
        "low": "float64[pyarrow]",
        "close": "float64[pyarrow]",
        "volume": "float64[pyarrow]",
    }

    def __init__(self, data_dir: str | None = None) -> None:
        self._data_dir = Path(data_dir or _DEFAULT_DATA_DIR)
        self._klines_dir = self._data_dir / "klines"
        self._klines_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, pd.DataFrame | None] = {}
        logger.info("KlineStore 就绪", path=str(self._klines_dir))

    # ── 文件路径 ────────────────────────────────

    def _file_path(self, symbol: str, timeframe: str) -> Path:
        """获取 Parquet 文件路径。"""
        return self._klines_dir / f"{symbol.upper()}_{timeframe}.parquet"

    # ── 写入 ────────────────────────────────────

    def append(self, symbol: str, timeframe: str, df: pd.DataFrame) -> int:
        """
        追加 K 线数据到 Parquet 文件。
        自动去重（按 ts 列）。
        
        参数:
            symbol: 交易对（如 BTCUSDT）
            timeframe: 周期（如 1h）
            df: 包含 open/high/low/close/volume/ts 的 DataFrame
        
        返回:
            写入的行数
        """
        if df.empty:
            return 0

        symbol = symbol.upper()
        df = df.copy()

        # 确保 ts 列为 int64 毫秒时间戳
        if "ts" not in df.columns:
            logger.error("DataFrame 缺少 ts 列")
            return 0

        # 标准化列
        for col in self.COLUMNS:
            if col not in df.columns:
                if col == "ts":
                    continue
                df[col] = 0.0

        # 只保留需要的列
        df = df[[c for c in self.COLUMNS if c in df.columns]]

        # 确保数值类型
        for col, dtype in self.DTYPES.items():
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        filepath = self._file_path(symbol, timeframe)

        if filepath.exists():
            try:
                existing = pd.read_parquet(filepath)
                # 去重：保留新数据，覆盖已有 ts
                combined = pd.concat([existing, df], ignore_index=True)
                combined = combined.drop_duplicates(subset=["ts"], keep="last")
                combined = combined.sort_values("ts").reset_index(drop=True)
                combined.to_parquet(filepath, index=False)
                new_rows = len(combined) - len(existing)
            except Exception as exc:
                logger.warning("读取已有 Parquet 失败，覆盖写入", error=str(exc))
                df.to_parquet(filepath, index=False)
                new_rows = len(df)
        else:
            df.to_parquet(filepath, index=False)
            new_rows = len(df)

        # 清空缓存
        self._cache.pop(f"{symbol}_{timeframe}", None)

        logger.debug(
            "K 线数据已写入",
            symbol=symbol,
            timeframe=timeframe,
            rows=new_rows,
            file=str(filepath),
        )
        return new_rows

    # ── 查询 ────────────────────────────────────

    def query(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 300,
        since: int | None = None,
        until: int | None = None,
    ) -> pd.DataFrame:
        """
        查询历史 K 线数据。

        参数:
            symbol: 交易对
            timeframe: 周期
            limit: 返回行数（默认 300）
            since: 起始时间戳（毫秒，可选）
            until: 结束时间戳（毫秒，可选）

        返回:
            包含 COLUMNS 列的 DataFrame，按 ts 升序
        """
        symbol = symbol.upper()
        filepath = self._file_path(symbol, timeframe)

        if not filepath.exists():
            logger.warning("K 线数据文件不存在", file=str(filepath))
            return pd.DataFrame(columns=self.COLUMNS)

        # 尝试从缓存读取
        cache_key = f"{symbol}_{timeframe}"
        if cache_key in self._cache and self._cache[cache_key] is not None:
            df = self._cache[cache_key]
        else:
            try:
                df = pd.read_parquet(filepath)
                self._cache[cache_key] = df
            except Exception as exc:
                logger.error("读取 Parquet 失败", error=str(exc))
                return pd.DataFrame(columns=self.COLUMNS)

        if df.empty:
            return df

        # 时间过滤
        if since is not None:
            df = df[df["ts"] >= since]
        if until is not None:
            df = df[df["ts"] <= until]

        # 按时间降序取 limit 条，再升序返回
        df = df.sort_values("ts", ascending=False).head(limit)
        df = df.sort_values("ts").reset_index(drop=True)

        return df

    # ── 统计 ────────────────────────────────────

    def stats(self, symbol: str, timeframe: str) -> dict[str, Any]:
        """查询存储统计信息。"""
        filepath = self._file_path(symbol, timeframe)
        if not filepath.exists():
            return {"exists": False, "rows": 0, "file_size_mb": 0}

        try:
            df = pd.read_parquet(filepath)
            size_mb = filepath.stat().st_size / (1024 * 1024)
            return {
                "exists": True,
                "rows": len(df),
                "file_size_mb": round(size_mb, 2),
                "earliest_ts": int(df["ts"].min()) if not df.empty else None,
                "latest_ts": int(df["ts"].max()) if not df.empty else None,
                "date_range_days": (
                    (pd.to_datetime(df["ts"].max(), unit="ms") -
                     pd.to_datetime(df["ts"].min(), unit="ms")).days
                    if not df.empty else 0
                ),
            }
        except Exception as exc:
            logger.error("读取统计失败", error=str(exc))
            return {"exists": True, "error": str(exc)}

    def list_symbols(self) -> list[dict[str, Any]]:
        """列出所有已存储的交易对和周期。"""
        results: list[dict[str, Any]] = []
        for fpath in self._klines_dir.glob("*.parquet"):
            name = fpath.stem  # BTCUSDT_1h
            parts = name.split("_")
            if len(parts) >= 2:
                symbol = parts[0]
                timeframe = "_".join(parts[1:])
                stats = self.stats(symbol, timeframe)
                stats["symbol"] = symbol
                stats["timeframe"] = timeframe
                results.append(stats)
        return sorted(results, key=lambda x: x.get("symbol", ""))

    # ── 维护 ────────────────────────────────────

    def vacuum(self, symbol: str, timeframe: str, max_rows: int = 10000) -> int:
        """
        清理旧数据，只保留最新的 max_rows 条。

        返回:
            删除的行数
        """
        filepath = self._file_path(symbol, timeframe)
        if not filepath.exists():
            return 0

        try:
            df = pd.read_parquet(filepath)
            if len(df) <= max_rows:
                return 0

            df = df.sort_values("ts", ascending=False).head(max_rows)
            df = df.sort_values("ts").reset_index(drop=True)
            df.to_parquet(filepath, index=False)
            self._cache.pop(f"{symbol}_{timeframe}", None)

            deleted = len(pd.read_parquet(filepath)) - len(df)
            logger.info("清理旧 K 线数据", symbol=symbol, timeframe=timeframe, deleted=abs(deleted))
            return abs(deleted)
        except Exception as exc:
            logger.error("清理失败", error=str(exc))
            return 0

    def close(self) -> None:
        """清理资源。"""
        self._cache.clear()
        logger.debug("KlineStore 已关闭")


__all__ = [
    "KlineStore",
]
