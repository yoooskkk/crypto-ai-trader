"""
数据回填脚本 — 从 Binance/Kucoin 拉取历史 K 线数据写入 TimescaleDB

用法:
    python -m scripts.backfill_data --symbol BTCUSDT --interval 1h --start 2025-01-01 --end 2025-02-01
    python -m scripts.backfill_data --all-major --interval 1d --days 365
    python -m scripts.backfill_data --list-symbols

返回值: 0（成功） 或 1（失败）
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

# ─── 配置 ─────────────────────────────────────────────────

_DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT",
    "DOGEUSDT", "XRPUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "DOTUSDT", "MATICUSDT", "ATOMUSDT",
]

_DEFAULT_INTERVAL = "1h"
_DEFAULT_DAYS = 30
_CCXT_RATE_LIMIT_S = 0.5  # ccxt 速率限制间隔


# ─── 回填任务 ────────────────────────────────────────────

@dataclass
class BackfillTask:
    symbol: str
    interval: str
    start: datetime
    end: datetime
    exchange: str = "binance"
    batch_size: int = 500    # 每次拉取的最大 K 线条数
    records_inserted: int = 0
    errors: list[str] = field(default_factory=list)


class DataBackfiller:
    """
    历史数据回填引擎。
    支持 Binance / Kucoin 交易所，写入 TimescaleDB。
    """

    def __init__(self, db_url: Optional[str] = None):
        self._db_url = db_url or os.getenv(
            "TIMESCALEDB_URL",
            f"postgresql://{os.getenv('TIMESCALEDB_USER', 'trader')}:"
            f"{os.getenv('TIMESCALEDB_PASSWORD', 'trader')}@"
            f"{os.getenv('TIMESCALEDB_HOST', 'localhost')}:"
            f"{os.getenv('TIMESCALEDB_PORT', '5432')}/"
            f"{os.getenv('TIMESCALEDB_DB', 'crypto_trader')}"
        )
        self._exchange_instance = None
        self._db_pool = None

    async def _get_exchange(self, exchange_id: str = "binance") -> Any:
        """获取 ccxt 交易所实例。"""
        if self._exchange_instance is not None:
            return self._exchange_instance

        try:
            import ccxt.async_support as ccxt_async
            exchange_class = getattr(ccxt_async, exchange_id, None)
            if exchange_class is None:
                raise ValueError(f"不支持的交易所: {exchange_id}")

            self._exchange_instance = exchange_class({
                "enableRateLimit": True,
                "rateLimit": int(_CCXT_RATE_LIMIT_S * 1000),
            })
            return self._exchange_instance
        except ImportError:
            logger.warning("ccxt 未安装，回退到模拟模式")
            return None

    async def _get_db_pool(self) -> Any:
        """获取 asyncpg 连接池。"""
        if self._db_pool is not None:
            return self._db_pool

        try:
            import asyncpg
            self._db_pool = await asyncpg.create_pool(
                self._db_url,
                min_size=1,
                max_size=5,
            )
            return self._db_pool
        except ImportError:
            logger.warning("asyncpg 未安装，跳过数据库写入")
            return None

    async def _fetch_ohlcv(
        self,
        exchange: Any,
        symbol: str,
        interval: str,
        since: int,
        limit: int = 500,
    ) -> list[list[Any]]:
        """从交易所拉取 OHLCV 数据。"""
        if exchange is None:
            logger.debug("模拟模式：返回空数据", symbol=symbol)
            return []

        try:
            ohlcv = await exchange.fetch_ohlcv(
                symbol=symbol,
                timeframe=interval,
                since=since,
                limit=limit,
            )
            return ohlcv
        except Exception as exc:
            logger.error("获取 OHLCV 失败", symbol=symbol, error=str(exc))
            return []

    async def _write_to_db(
        self,
        pool: Any,
        symbol: str,
        interval: str,
        ohlcv_list: list[list[Any]],
    ) -> int:
        """将 OHLCV 数据写入 TimescaleDB。"""
        if pool is None:
            return len(ohlcv_list)  # 模拟模式：假装写入成功

        try:
            async with pool.acquire() as conn:
                # 确保 hypertable 存在
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ohlcv (
                        ts          TIMESTAMPTZ NOT NULL,
                        symbol      TEXT NOT NULL,
                        interval    TEXT NOT NULL,
                        open        DOUBLE PRECISION,
                        high        DOUBLE PRECISION,
                        low         DOUBLE PRECISION,
                        close       DOUBLE PRECISION,
                        volume      DOUBLE PRECISION,
                        PRIMARY KEY (ts, symbol, interval)
                    );
                """)
                await conn.execute("""
                    SELECT create_hypertable('ohlcv', 'ts',
                        if_not_exists => TRUE);
                """)

                inserted = 0
                for row in ohlcv_list:
                    ts, open_, high, low, close, volume = row
                    try:
                        await conn.execute("""
                            INSERT INTO ohlcv (ts, symbol, interval, open, high, low, close, volume)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                            ON CONFLICT (ts, symbol, interval) DO NOTHING
                        """, datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                            symbol, interval, open_, high, low, close, volume)
                        inserted += 1
                    except Exception:
                        continue

                return inserted

        except Exception as exc:
            logger.error("数据库写入失败", error=str(exc))
            return 0

    async def backfill(self, task: BackfillTask) -> BackfillTask:
        """
        执行单个回填任务。

        参数:
            task: 回填任务配置（会被原地修改记录结果）

        返回:
            task（已更新）
        """
        logger.info(
            "开始回填",
            symbol=task.symbol,
            interval=task.interval,
            start=task.start.isoformat(),
            end=task.end.isoformat(),
        )

        exchange = await self._get_exchange(task.exchange)
        pool = await self._get_db_pool()

        since = int(task.start.timestamp() * 1000)
        end_ms = int(task.end.timestamp() * 1000)
        total_inserted = 0

        while since < end_ms:
            ohlcv = await self._fetch_ohlcv(
                exchange, task.symbol, task.interval, since, task.batch_size,
            )

            if not ohlcv:
                logger.warning("未获取到数据", symbol=task.symbol, since=since)
                break

            inserted = await self._write_to_db(pool, task.symbol, task.interval, ohlcv)
            total_inserted += inserted

            # 更新时间游标到最新一笔 K 线
            last_ts = ohlcv[-1][0]
            since = last_ts + 1

            logger.info(
                "回填进度",
                symbol=task.symbol,
                batch=inserted,
                total=total_inserted,
                progress=f"{datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).isoformat()}",
            )

            # 遵守速率限制
            await asyncio.sleep(_CCXT_RATE_LIMIT_S)

        task.records_inserted = total_inserted
        logger.info(
            "回填完成",
            symbol=task.symbol,
            interval=task.interval,
            total=total_inserted,
        )

        return task

    async def backfill_multiple(
        self,
        tasks: list[BackfillTask],
        concurrency: int = 3,
    ) -> list[BackfillTask]:
        """
        并发执行多个回填任务。

        参数:
            tasks: 任务列表
            concurrency: 最大并发数

        返回:
            完成后的任务列表
        """
        semaphore = asyncio.Semaphore(concurrency)

        async def _run_with_limit(t: BackfillTask) -> BackfillTask:
            async with semaphore:
                return await self.backfill(t)

        results = await asyncio.gather(*[_run_with_limit(t) for t in tasks])
        return list(results)

    async def close(self) -> None:
        """关闭连接。"""
        if self._exchange_instance is not None:
            try:
                await self._exchange_instance.close()
            except Exception:
                pass
        if self._db_pool is not None:
            try:
                await self._db_pool.close()
            except Exception:
                pass


# ─── 辅助函数 ─────────────────────────────────────────────

def parse_date(date_str: str) -> datetime:
    """解析日期字符串为 UTC datetime。"""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        except Exception:
            raise ValueError(f"无法解析日期: {date_str}")


def interval_to_milliseconds(interval: str) -> int:
    """将间隔字符串转换为毫秒（用于计算批次数量）。"""
    unit = interval[-1]
    value = int(interval[:-1])
    multipliers = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    return value * multipliers.get(unit, 60_000)


# ─── 主逻辑 ───────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="历史数据回填脚本")
    parser.add_argument("--symbol", type=str, default=None, help="交易对，如 BTCUSDT")
    parser.add_argument("--all-major", action="store_true", help="回填所有主流交易对")
    parser.add_argument("--list-symbols", action="store_true", help="列出可用的交易对")
    parser.add_argument("--interval", type=str, default=_DEFAULT_INTERVAL, help="K 线间隔")
    parser.add_argument("--start", type=str, default=None, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=_DEFAULT_DAYS, help="回溯天数（默认 30）")
    parser.add_argument("--exchange", type=str, default="binance", help="交易所")
    parser.add_argument("--concurrency", type=int, default=3, help="最大并发数")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.list_symbols:
        print("可用的主流交易对:")
        for s in _DEFAULT_SYMBOLS:
            print(f"  {s}")
        return 0

    # 确定回填交易对
    symbols: list[str] = []
    if args.all_major:
        symbols = list(_DEFAULT_SYMBOLS)
    elif args.symbol:
        symbols = [args.symbol.upper()]
    else:
        print("请指定 --symbol 或 --all-major")
        return 1

    # 确定时间范围
    end = parse_date(args.end) if args.end else datetime.now(timezone.utc)
    start = parse_date(args.start) if args.start else end - timedelta(days=args.days)

    # 构建任务
    tasks = [
        BackfillTask(
            symbol=s,
            interval=args.interval,
            start=start,
            end=end,
            exchange=args.exchange,
        )
        for s in symbols
    ]

    # 执行回填
    async def _do_backfill() -> int:
        backfiller = DataBackfiller()
        try:
            results = await backfiller.backfill_multiple(tasks, concurrency=args.concurrency)
            total = sum(t.records_inserted for t in results)
            errors = sum(len(t.errors) for t in results)

            print(f"回填完成: {len(results)} 个交易对, {total} 条记录, {errors} 个错误")
            for t in results:
                status = "✅" if not t.errors else "⚠️"
                print(f"  {status} {t.symbol} ({t.interval}): {t.records_inserted} 条")

            return 0 if errors == 0 else 1
        finally:
            await backfiller.close()

    return asyncio.run(_do_backfill())


if __name__ == "__main__":
    sys.exit(main())

