"""
DataBackfiller 测试套件
覆盖：任务构建 / 爬取 / 写入 / 主入口
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scripts.backfill_data import (
    BackfillTask,
    DataBackfiller,
    parse_date,
    interval_to_milliseconds,
    parse_args,
    main,
)


# ─── 辅助函数测试 ─────────────────────────────────────

class TestParseDate:
    def test_ymd_format(self):
        d = parse_date("2025-01-15")
        assert d.year == 2025
        assert d.month == 1
        assert d.day == 15

    def test_iso_format(self):
        d = parse_date("2025-01-15T12:00:00")
        assert d.hour == 12

    def test_raises_on_invalid(self):
        with pytest.raises(ValueError):
            parse_date("not-a-date")


class TestIntervalToMilliseconds:
    def test_minutes(self):
        assert interval_to_milliseconds("15m") == 15 * 60_000

    def test_hours(self):
        assert interval_to_milliseconds("1h") == 3_600_000

    def test_days(self):
        assert interval_to_milliseconds("1d") == 86_400_000


# ─── BackfillTask 测试 ─────────────────────────────────

class TestBackfillTask:
    def test_defaults(self):
        from datetime import datetime, timezone
        task = BackfillTask(
            symbol="BTCUSDT",
            interval="1h",
            start=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end=datetime(2025, 2, 1, tzinfo=timezone.utc),
        )
        assert task.symbol == "BTCUSDT"
        assert task.records_inserted == 0
        assert task.errors == []


# ─── DataBackfiller 测试 ────────────────────────────────

class TestDataBackfiller:
    def test_init(self):
        bf = DataBackfiller()
        assert bf is not None

    def test_get_exchange_import_error(self):
        bf = DataBackfiller()
        with patch.dict("sys.modules", {"ccxt.async_support": None}):
            exc = bf._get_exchange()
            import asyncio
            result = asyncio.run(exc)
            assert result is None

    def test_get_db_pool_import_error(self):
        bf = DataBackfiller()
        with patch.dict("sys.modules", {"asyncpg": None}):
            result = bf._get_db_pool()
            import asyncio
            pool = asyncio.run(result)
            assert pool is None

    def test_fetch_ohlcv_no_exchange(self):
        bf = DataBackfiller()
        import asyncio
        result = asyncio.run(bf._fetch_ohlcv(None, "BTCUSDT", "1h", 0))
        assert result == []

    def test_write_to_db_no_pool(self):
        bf = DataBackfiller()
        import asyncio
        count = asyncio.run(bf._write_to_db(None, "BTCUSDT", "1h", [[1, 2, 3, 4, 5, 6]]))
        assert count == 1

    def test_backfill_empty_exchange(self):
        """无交易所时，backfill 应优雅返回。"""
        bf = DataBackfiller()
        from datetime import datetime, timezone
        task = BackfillTask(
            symbol="BTCUSDT",
            interval="1h",
            start=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end=datetime(2025, 1, 2, tzinfo=timezone.utc),
        )
        import asyncio
        result = asyncio.run(bf.backfill(task))
        assert result.records_inserted == 0
        assert result.symbol == "BTCUSDT"

    def test_close(self):
        bf = DataBackfiller()
        import asyncio
        asyncio.run(bf.close())  # 不抛异常即可

    def test_backfill_multiple_empty(self):
        bf = DataBackfiller()
        import asyncio
        from datetime import datetime, timezone
        tasks = [
            BackfillTask(symbol=f"TEST{i}", interval="1h",
                         start=datetime(2025, 1, 1, tzinfo=timezone.utc),
                         end=datetime(2025, 1, 2, tzinfo=timezone.utc))
            for i in range(3)
        ]
        results = asyncio.run(bf.backfill_multiple(tasks, concurrency=2))
        assert len(results) == 3


# ─── CLI 测试 ──────────────────────────────────────────

class TestCLI:
    def test_list_symbols(self):
        with patch("sys.argv", ["backfill_data.py", "--list-symbols"]):
            rc = main()
            assert rc == 0

    def test_missing_symbol(self):
        """不指定 symbol 应返回 1。"""
        with patch("sys.argv", ["backfill_data.py"]):
            rc = main()
            assert rc == 1

    def test_single_symbol(self):
        """指定 symbol 应执行回填并返回 0。"""
        with patch("sys.argv", ["backfill_data.py", "--symbol", "BTCUSDT", "--days", "1"]):
            rc = main()
            assert rc == 0

    def test_all_major(self):
        with patch("sys.argv", ["backfill_data.py", "--all-major", "--days", "1"]):
            rc = main()
            assert rc == 0

    def test_custom_interval(self):
        with patch("sys.argv", ["backfill_data.py", "--symbol", "ETHUSDT",
                                "--interval", "1d", "--days", "1"]):
            rc = main()
            assert rc == 0

    def test_custom_dates(self):
        with patch("sys.argv", ["backfill_data.py", "--symbol", "SOLUSDT",
                                "--start", "2025-01-01", "--end", "2025-01-02"]):
            rc = main()
            assert rc == 0
