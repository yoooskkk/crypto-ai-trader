"""
决策链路日志测试。
覆盖：DecisionRecord 构造、日志写入、DB 写入（mock asyncpg）、查询、降级行为。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from observability.decision_logger import (
    DecisionLogger,
    DecisionRecord,
    _DB_CONFIG,
    _INSERT_SQL,
)


# ─── 辅助函数 ────────────────────────────────────


class _MockAcquireContext:
    """模拟 asyncpg 连接池的 acquire() 返回的 async 上下文管理器。"""
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        pass


class _MockPool:
    """模拟 asyncpg 连接池。"""
    def __init__(self):
        self.conn = AsyncMock()
        self.acquire_called = False

    def acquire(self):
        self.acquire_called = True
        return _MockAcquireContext(self.conn)

    async def close(self):
        pass


def make_record(**kwargs) -> DecisionRecord:
    """生成测试用 DecisionRecord。"""
    defaults = dict(
        ts="2025-06-01T10:00:00+00:00",
        symbol="BTCUSDT",
        timeframe="1h",
        prompt_version="market_analysis=abc123",
        regime="trending",
        raw_llm_output='{"direction": "LONG", "confidence": 0.8}',
        validated=True,
        direction="LONG",
        confidence=0.8,
        breaker_state="CLOSED",
        signal_sent=True,
    )
    defaults.update(kwargs)
    return DecisionRecord(**defaults)


# ═══════════════════════════════════════════════════
# 1. DecisionRecord
# ═══════════════════════════════════════════════════

class TestDecisionRecord:
    """决策记录模型测试。"""

    def test_basic_construction(self):
        """基础构造应正确。"""
        record = make_record()
        assert record.symbol == "BTCUSDT"
        assert record.direction == "LONG"
        assert record.validated is True

    def test_from_plan(self):
        """from_plan 应自动填充时间戳。"""
        record = DecisionRecord.from_plan(
            symbol="ETHUSDT",
            timeframe="4h",
            prompt_version="v2",
            regime="ranging",
            raw_llm_output="{}",
            validated=True,
            direction="SHORT",
            confidence=0.65,
        )
        assert record.symbol == "ETHUSDT"
        assert record.direction == "SHORT"
        assert record.ts != ""  # 自动填充
        assert record.breaker_state == "CLOSED"  # 默认值
        assert record.signal_sent is False  # 默认值

    def test_from_plan_with_overrides(self):
        """from_plan 应接受所有显式参数覆盖默认。"""
        record = DecisionRecord.from_plan(
            symbol="SOLUSDT",
            timeframe="15m",
            prompt_version="v3",
            regime="crash",
            raw_llm_output="{\"dir\": \"FLAT\"}",
            validated=False,
            direction=None,
            confidence=None,
            breaker_state="OPEN",
            signal_sent=False,
        )
        assert record.breaker_state == "OPEN"
        assert record.direction is None
        assert record.signal_sent is False

    def test_to_dict(self):
        """to_dict 应返回所有字段。"""
        record = make_record()
        d = record.to_dict()
        assert isinstance(d, dict)
        assert d["symbol"] == "BTCUSDT"
        assert d["direction"] == "LONG"


# ═══════════════════════════════════════════════════
# 2. DecisionLogger — 基础功能
# ═══════════════════════════════════════════════════

class TestDecisionLogger:
    """决策日志器测试。"""

    def test_init_no_asyncpg(self):
        """asyncpg 不可用时不应崩溃，pool 为 None。"""
        dl = DecisionLogger()
        dl._asyncpg_available = False  # 模拟 asyncpg 不可用
        assert dl._pool is None

    def test_connect_no_asyncpg(self):
        """asyncpg 不可用时 connect 应为空操作。"""
        dl = DecisionLogger()
        dl._asyncpg_available = False
        import asyncio
        asyncio.run(dl.connect())
        assert dl._pool is None

    def test_close_no_pool(self):
        """无连接池时 close 应为空操作。"""
        dl = DecisionLogger()
        import asyncio
        asyncio.run(dl.close())  # 不应抛出异常

    def test_connected_property(self):
        """connected 属性应正确反映连接池状态。"""
        dl = DecisionLogger()
        assert dl.connected is False

    def test_log_no_db(self):
        """无 DB 时 log 仅输出控制台日志。"""
        dl = DecisionLogger()
        dl._asyncpg_available = False
        record = make_record()
        import asyncio
        asyncio.run(dl.log(record))
        # 不应抛出异常

    def test_custom_db_config(self):
        """自定义 DB 配置应覆盖默认。"""
        dl = DecisionLogger(db_config={"host": "timescale.local", "port": 5433})
        assert dl._db_config["host"] == "timescale.local"
        assert dl._db_config["port"] == 5433
        # 未覆盖的值应保留默认
        assert dl._db_config["user"] == "trader"


# ═══════════════════════════════════════════════════
# 3. DecisionLogger — DB 写入（mock asyncpg）
# ═══════════════════════════════════════════════════

class TestDecisionLoggerDB:
    """带有 mock DB 的日志写入测试。"""

    @pytest.fixture
    def mock_logger(self):
        """创建带模拟连接池的 DecisionLogger。"""
        dl = DecisionLogger()
        dl._asyncpg_available = True
        mock_pool = _MockPool()
        dl._pool = mock_pool
        return dl, mock_pool, mock_pool.conn

    def test_connect_sets_pool(self):
        """connect 设置 _pool 后 connected 应为 True。"""
        dl = DecisionLogger()
        dl._pool = _MockPool()  # 直接注入模拟连接池
        assert dl.connected is True

    def test_write_db(self, mock_logger):
        """log 应正确调用 INSERT。"""
        dl, _mock_pool, mock_conn = mock_logger
        record = make_record()
        import asyncio
        asyncio.run(dl.log(record))

        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args[0]
        assert "decision_log" in call_args[0]

    def test_write_db_failure_logs_error(self, mock_logger):
        """DB 写入失败应记录错误但不崩溃。"""
        dl, _mock_pool, mock_conn = mock_logger
        mock_conn.execute.side_effect = Exception("DB timeout")

        record = make_record()
        import asyncio
        asyncio.run(dl.log(record))
        # 不应抛出异常

    def test_fetch_recent(self, mock_logger):
        """fetch_recent 应返回格式化的查询结果。"""
        dl, _mock_pool, mock_conn = mock_logger
        mock_conn.fetch.return_value = [
            {"ts": "2025-06-01T10:00:00", "symbol": "BTCUSDT", "validated": True},
        ]

        import asyncio
        results = asyncio.run(dl.fetch_recent(limit=5))

        assert len(results) == 1
        assert results[0]["symbol"] == "BTCUSDT"
        mock_conn.fetch.assert_called_once()

    def test_fetch_recent_with_filters(self, mock_logger):
        """fetch_recent 应支持按 symbol 和 validated 过滤。"""
        dl, _mock_pool, mock_conn = mock_logger
        mock_conn.fetch.return_value = []

        import asyncio
        results = asyncio.run(dl.fetch_recent(
            limit=10, symbol="ETHUSDT", validated_only=True,
        ))
        assert isinstance(results, list)

    def test_fetch_recent_no_connection(self):
        """无连接时 fetch_recent 应返回空列表。"""
        dl = DecisionLogger()
        import asyncio
        results = asyncio.run(dl.fetch_recent())
        assert results == []

    def test_write_db_after_close(self, mock_logger):
        """关闭连接池后写入不应崩溃。"""
        dl, _mock_pool, _ = mock_logger
        import asyncio
        asyncio.run(dl.close())
        record = make_record()
        asyncio.run(dl.log(record))  # 不应崩溃
        assert dl.connected is False


# ═══════════════════════════════════════════════════
# 4. INSERT SQL 格式
# ═══════════════════════════════════════════════════

class TestInsertSQL:
    """SQL 模板测试。"""

    def test_sql_includes_all_columns(self):
        """SQL 应包含所有 decision_log 列。"""
        assert "ts" in _INSERT_SQL
        assert "symbol" in _INSERT_SQL
        assert "timeframe" in _INSERT_SQL
        assert "prompt_version" in _INSERT_SQL
        assert "regime" in _INSERT_SQL
        assert "validated" in _INSERT_SQL
        assert "direction" in _INSERT_SQL
        assert "confidence" in _INSERT_SQL
        assert "breaker_state" in _INSERT_SQL
        assert "signal_sent" in _INSERT_SQL
        assert "raw_output" in _INSERT_SQL
        assert _INSERT_SQL.startswith("INSERT INTO decision_log")

    def test_sql_parameter_count(self):
        """SQL 参数占位符数量应等于列数。"""
        param_count = _INSERT_SQL.count("$")
        # 11 列 = 11 个参数
        assert param_count == 11
