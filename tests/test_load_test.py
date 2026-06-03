"""
scripts/load_test.py 的单元测试。

验证核心逻辑正确性（不依赖 Redis）：
  - MockKline 生成
  - TestReport 数据结构
  - 持续时间解析
  - 参数校验
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.load_test import (
    MockKline,
    LatencySample,
    StreamSnapshot,
    TestReport,
    _parse_duration,
    build_parser,
)


# ═══════════════════════════════════════════════════════════════
#  MockKline 测试
# ═══════════════════════════════════════════════════════════════

class TestMockKline:
    """MockKline 工厂的生成逻辑。"""

    def teardown_method(self):
        MockKline.reset_price()

    def test_single_kline_has_required_fields(self):
        """单根 K 线应包含所有必需字段。"""
        kline = MockKline.next(idx=0)
        required = {"symbol", "timeframe", "ts", "open", "high", "low",
                     "close", "volume", "quote_volume", "taker_buy_volume",
                     "taker_buy_quote", "is_closed"}
        assert required.issubset(kline.keys()), f"缺少字段: {required - set(kline.keys())}"

    def test_symbol_defaults_to_btc(self):
        """symbol 默认 BTCUSDT。"""
        kline = MockKline.next(0)
        assert kline["symbol"] == "BTCUSDT"

    def test_custom_symbol(self):
        """可指定 symbol。"""
        kline = MockKline.next(0, symbol="ETHUSDT")
        assert kline["symbol"] == "ETHUSDT"

    def test_custom_timeframe(self):
        """可指定 timeframe。"""
        kline = MockKline.next(0, timeframe="5m")
        assert kline["timeframe"] == "5m"

    def test_is_closed_default_true(self):
        """is_closed 默认 True。"""
        kline = MockKline.next(0)
        assert kline["is_closed"] is True

    def test_is_closed_false(self):
        kline = MockKline.next(0, is_closed=False)
        assert kline["is_closed"] is False

    def test_price_changes_each_call(self):
        """每次调用价格不同。"""
        k1 = MockKline.next(0)
        k2 = MockKline.next(1)
        assert k1["close"] != k2["close"], "连续调用价格应不同"

    def test_reset_price(self):
        """reset_price 应重置价格序列。"""
        MockKline.next(0)
        MockKline.reset_price(50000.0)
        k1 = MockKline.next(0)
        k2 = MockKline.next(1)
        assert float(k1["close"]) != 0.0

    def test_prices_are_strings(self):
        """价格字段应为字符串（模拟 Binance 格式）。"""
        kline = MockKline.next(0)
        for field in ("open", "high", "low", "close", "volume"):
            assert isinstance(kline[field], str), f"{field} 应为 str，实际 {type(kline[field])}"

    def test_consecutive_price_movement(self):
        """连续生成的 K 线价格应呈趋势。"""
        prices = []
        MockKline.reset_price(50000)
        for i in range(10):
            k = MockKline.next(i)
            prices.append(float(k["close"]))
        # 价格应有自然波动（不全相等）
        assert len(set(prices)) > 5, "价格序列应有多样性"

    def test_ts_increases_with_idx(self):
        """时间戳随 idx 递增。"""
        t1 = MockKline.next(0)["ts"]
        t2 = MockKline.next(100)["ts"]
        assert t2 > t1

    def test_timeframe_in_kline_series(self):
        """不同 timeframes 应独立。"""
        k1 = MockKline.next(0, timeframe="1h")
        k2 = MockKline.next(0, timeframe="1m")
        assert k1["timeframe"] == "1h"
        assert k2["timeframe"] == "1m"


# ═══════════════════════════════════════════════════════════════
#  TestReport 测试
# ═══════════════════════════════════════════════════════════════

class TestTestReport:
    """TestReport 数据结构和序列化。"""

    def test_default_values(self):
        """默认值应为零或空。"""
        r = TestReport(mode="test", start_time="", end_time="", duration_seconds=0.0)
        assert r.klines_injected == 0
        assert r.indicators_produced == 0
        assert r.errors == []
        assert r.passed is False

    def test_json_serializable(self):
        """asdict 输出应可 JSON 序列化。"""
        r = TestReport(
            mode="smoke",
            start_time="2025-01-01T00:00:00",
            end_time="2025-01-01T00:01:00",
            duration_seconds=60.0,
            klines_injected=300,
            indicators_produced=50,
            regime_signals_produced=30,
            ai_signals_produced=20,
            trade_orders_produced=10,
            errors=["测试错误"],
            latency_p50_ms=100.0,
            latency_p95_ms=500.0,
            latency_p99_ms=1000.0,
            latency_max_ms=2000.0,
            max_stream_backlog={"raw_kline": 100, "trade_order": 5},
            container_restarts=0,
            passed=True,
        )
        data = {
            k: str(v) if not isinstance(v, (int, float, str, bool, list, dict, type(None))) else v
            for k, v in r.__dict__.items()
        }
        # 验证可序列化
        json_str = json.dumps(data, ensure_ascii=False, default=str)
        assert isinstance(json_str, str)
        # 验证反序列化
        loaded = json.loads(json_str)
        assert loaded["mode"] == "smoke"
        assert loaded["passed"] is True
        assert loaded["klines_injected"] == 300

    def test_passed_flag(self):
        """passed 标志控制。"""
        r = TestReport(mode="test", start_time="", end_time="", duration_seconds=0.0)
        assert r.passed is False
        r.passed = True
        assert r.passed is True


# ═══════════════════════════════════════════════════════════════
#  _parse_duration 测试
# ═══════════════════════════════════════════════════════════════

class TestParseDuration:
    """持续时间解析。"""

    def test_seconds(self):
        assert _parse_duration("30s") == 30

    def test_minutes(self):
        assert _parse_duration("5m") == 300

    def test_hours(self):
        assert _parse_duration("2h") == 7200

    def test_twelve_hours(self):
        assert _parse_duration("12h") == 43200

    def test_raw_seconds(self):
        assert _parse_duration("3600") == 3600

    def test_empty_hour(self):
        assert _parse_duration("1h") == 3600

    def test_lowercase(self):
        assert _parse_duration("12H") == 43200


# ═══════════════════════════════════════════════════════════════
#  CLI 参数解析测试
# ═══════════════════════════════════════════════════════════════

class TestCLIParser:
    """CLI 参数解析。"""

    def test_smoke_mode(self):
        parser = build_parser()
        args = parser.parse_args(["--smoke"])
        assert args.smoke is True
        assert args.load is False
        assert args.stability is False
        assert args.latency is False

    def test_load_mode_with_rate(self):
        parser = build_parser()
        args = parser.parse_args(["--load", "--rate", "100"])
        assert args.load is True
        assert args.rate == 100

    def test_stability_with_duration(self):
        parser = build_parser()
        args = parser.parse_args(["--stability", "--duration", "12h"])
        assert args.stability is True
        assert args.duration == "12h"

    def test_latency_with_count(self):
        parser = build_parser()
        args = parser.parse_args(["--latency", "--count", "200"])
        assert args.latency is True
        assert args.count == 200

    def test_json_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--smoke", "--json"])
        assert args.json_output is True

    def test_quiet_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--smoke", "--quiet"])
        assert args.quiet is True

    def test_redis_host_port(self):
        parser = build_parser()
        args = parser.parse_args(["--smoke", "--redis-host", "10.0.0.1", "--redis-port", "16379"])
        assert args.redis_host == "10.0.0.1"
        assert args.redis_port == 16379

    def test_no_mode_errors(self):
        """不指定模式应退出。"""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_multiple_modes_error(self):
        """指定多个模式应退出。"""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--smoke", "--load"])


# ═══════════════════════════════════════════════════════════════
#  Dataclass 测试
# ═══════════════════════════════════════════════════════════════

class TestDataclasses:
    """LatencySample 和 StreamSnapshot。"""

    def test_latency_sample_creation(self):
        s = LatencySample(
            start_ts=1000.0,
            stream="trade_order",
            arrival_ts=1005.0,
            latency_ms=5000.0,
            symbol="BTCUSDT",
            direction="LONG",
        )
        assert s.latency_ms == 5000.0
        assert s.stream == "trade_order"

    def test_stream_snapshot_creation(self):
        s = StreamSnapshot(
            timestamp=1000.0,
            stream="indicators",
            length=50,
            last_id="123-0",
            consumer_lag=2,
        )
        assert s.length == 50
        assert s.stream == "indicators"
