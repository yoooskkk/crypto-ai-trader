"""
风险控制层综合测试。
覆盖：drawdown_limit, position_sizer, exposure_monitor, signal_arbiter, circuit_breaker, AiSignalStrategy
"""
from __future__ import annotations

import json
import math
import time
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from risk_guardian.drawdown_limit import DrawdownLimit, DrawdownLevel, _load_drawdown_limits
from risk_guardian.position_sizer import PositionSizer, REGIME_MULTIPLIER, MAX_KELLY_FRACTION
from risk_guardian.exposure_monitor import ExposureMonitor, ExposureSnapshot, PositionInfo
from risk_guardian.signal_arbiter import SignalArbiter, ArbitratedOrder


@pytest.fixture
def mock_breaker():
    """Mock CircuitBreaker 夹具。"""
    b = MagicMock()
    b.allow_open.return_value = True
    b.state.value = "closed"
    return b


@pytest.fixture
def mock_sizer():
    """Mock PositionSizer 夹具。"""
    s = MagicMock()
    s.calculate.return_value = 0.08
    return s


@pytest.fixture
def mock_drawdown():
    """Mock DrawdownLimit 夹具。"""
    d = MagicMock()
    d.can_open_position.return_value = True
    d.check_limits.return_value = {"level": "NORMAL"}
    d.get_position_multiplier.return_value = 1.0
    return d


# ═══════════════════════════════════════════════════════════════
# drawdown_limit.py 测试
# ═══════════════════════════════════════════════════════════════

class TestDrawdownLimit:
    """DrawdownLimit 回撤追踪器测试。"""

    def test_initial_state(self):
        """初始状态应为 NORMAL，允许开仓。"""
        dd = DrawdownLimit()
        status = dd.check_limits()
        assert status["level"] == DrawdownLevel.NORMAL
        assert status["allow_new"] is True
        assert status["force_exit"] is False
        assert dd.can_open_position() is True

    def test_daily_drawdown_triggers_daily_level(self):
        """日回撤超过 daily_max_pct 应触发 DAILY 等级。"""
        dd = DrawdownLimit(daily_max_pct=5.0)
        peak = 10000.0
        current = 9400.0  # 6% 回撤 > 5%
        dd.update(peak_equity=peak, current_equity=current)
        status = dd.check_limits()
        assert status["level"] == DrawdownLevel.DAILY
        assert status["allow_new"] is False
        assert status["drawdown_pct"] == pytest.approx(6.0, rel=0.1)

    def test_weekly_drawdown_triggers_weekly_level(self):
        """周回撤超过 weekly_max_pct 应触发 WEEKLY 等级。"""
        dd = DrawdownLimit(weekly_max_pct=10.0)
        dd._week_start_eq = 10000.0
        dd.peak_equity = 10000.0
        current = 8800.0  # 12% 回撤 > 10% 且 < 15%（月阈值），应触发 WEEKLY
        dd.update(peak_equity=10000.0, current_equity=current)
        status = dd.check_limits()
        assert status["level"] == DrawdownLevel.WEEKLY
        assert status["allow_new"] is False

    def test_monthly_drawdown_triggers_monthly_level_and_force_exit(self):
        """月回撤超过 monthly_max_pct 应触发 MONTHLY 等级 + force_exit"""
        dd = DrawdownLimit(monthly_max_pct=15.0)
        dd._month_start_eq = 10000.0
        dd.peak_equity = 10000.0
        current = 8000.0  # 20% 回撤 > 15%
        dd.update(peak_equity=10000.0, current_equity=current)
        status = dd.check_limits()
        assert status["level"] == DrawdownLevel.MONTHLY
        assert status["allow_new"] is False
        assert status["force_exit"] is True

    def test_normal_drawdown_allows_positions(self):
        """小幅回撤（低于阈值）应允许开仓。"""
        dd = DrawdownLimit(daily_max_pct=5.0)
        dd.update(peak_equity=10000.0, current_equity=9800.0)  # 2% 回撤
        assert dd.can_open_position() is True
        assert dd.get_position_multiplier() == 1.0

    def test_daily_drawdown_blocks_positions(self):
        """日回撤超限后 get_position_multiplier 应返回 0.0"""
        dd = DrawdownLimit(daily_max_pct=5.0)
        dd.update(peak_equity=10000.0, current_equity=9300.0)  # 7% 回撤
        assert dd.get_position_multiplier() == 0.0

    def test_weekly_drawdown_halves_position(self):
        """周回撤超限后 get_position_multiplier 应返回 0.5"""
        dd = DrawdownLimit(weekly_max_pct=10.0)
        dd._week_start_eq = 10000.0
        dd.peak_equity = 10000.0
        dd.update(peak_equity=10000.0, current_equity=8800.0)  # 12% > 10%
        assert dd.get_position_multiplier() == 0.5

    def test_reset_clears_state(self):
        """reset() 应清除所有状态。"""
        dd = DrawdownLimit()
        dd.update(peak_equity=10000.0, current_equity=5000.0)
        dd.reset()
        assert dd.peak_equity == 0.0
        assert dd.can_open_position() is True

    def test_update_preserves_peak(self):
        """update 应跟踪历史最高净值。"""
        dd = DrawdownLimit()
        dd.update(peak_equity=10000.0, current_equity=10000.0)
        dd.update(peak_equity=11000.0, current_equity=11000.0)
        dd.update(peak_equity=11000.0, current_equity=10000.0)
        assert dd.peak_equity == 11000.0

    def test_from_config_uses_defaults_when_no_file(self):
        """from_config 在文件不存在时应使用默认参数。"""
        with patch("risk_guardian.drawdown_limit.RISK_CONFIG_PATH", "nonexistent.yml"):
            dd = DrawdownLimit.from_config()
            assert dd.daily_max_pct == 5.0
            assert dd.weekly_max_pct == 10.0
            assert dd.monthly_max_pct == 15.0

    def test_multiple_updates_no_double_warning(self):
        """多次更新不应重复触发告警级别（只计算一次）。"""
        dd = DrawdownLimit(daily_max_pct=5.0)
        dd.update(peak_equity=10000.0, current_equity=9300.0)  # 7%
        status1 = dd.check_limits()
        dd.update(peak_equity=10000.0, current_equity=9200.0)  # 8%
        status2 = dd.check_limits()
        assert status1["level"] == DrawdownLevel.DAILY
        assert status2["level"] == DrawdownLevel.DAILY


# ═══════════════════════════════════════════════════════════════
# position_sizer.py 测试
# ═══════════════════════════════════════════════════════════════

class TestPositionSizer:
    """PositionSizer Kelly 公式仓位计算器测试。"""

    def test_kelly_basic(self):
        """基本 Kelly 公式计算。"""
        sizer = PositionSizer(max_total_pct=80.0, max_single_pct=80.0)
        size = sizer.calculate(win_rate=0.6, avg_rr=2.0, regime="TRENDING", equity=10000.0)
        assert size == pytest.approx(0.25, rel=0.01)  # Kelly 25% 上限

    def test_kelly_no_edge(self):
        """无优势时 Kelly 应为 0。"""
        sizer = PositionSizer(max_total_pct=80.0, max_single_pct=80.0)
        size = sizer.calculate(win_rate=0.5, avg_rr=1.0, regime="TRENDING", equity=10000.0)
        assert size == 0.0

    def test_regime_multiplier_ranging(self):
        """RANGING 制度应将仓位减半。"""
        sizer = PositionSizer(max_total_pct=80.0, max_single_pct=80.0)
        size_trending = sizer.calculate(win_rate=0.6, avg_rr=3.0, regime="TRENDING", equity=10000.0)
        size_ranging = sizer.calculate(win_rate=0.6, avg_rr=3.0, regime="RANGING", equity=10000.0)
        assert size_ranging == pytest.approx(size_trending * 0.5, rel=0.01)

    def test_regime_multiplier_unknown(self):
        """UNKNOWN 制度应使用 0.25 倍。"""
        sizer = PositionSizer(max_total_pct=80.0, max_single_pct=80.0)
        size = sizer.calculate(win_rate=0.6, avg_rr=3.0, regime="UNKNOWN", equity=10000.0)
        assert size == pytest.approx(0.0625, rel=0.01)

    def test_high_volatility_multiplier(self):
        """HIGH_VOLATILITY 应使用 0.5 倍。"""
        sizer = PositionSizer(max_total_pct=80.0, max_single_pct=80.0)
        size_trending = sizer.calculate(win_rate=0.6, avg_rr=3.0, regime="TRENDING", equity=10000.0)
        size_high_vol = sizer.calculate(win_rate=0.6, avg_rr=3.0, regime="HIGH_VOLATILITY", equity=10000.0)
        assert size_high_vol == pytest.approx(size_trending * 0.5, rel=0.01)

    def test_kelly_capped_at_25_percent(self):
        """Kelly 分数不应超过 MAX_KELLY_FRACTION (25%)。"""
        sizer = PositionSizer(max_total_pct=80.0, max_single_pct=80.0)
        size = sizer.calculate(win_rate=0.9, avg_rr=10.0, regime="TRENDING", equity=10000.0)
        assert size <= MAX_KELLY_FRACTION

    def test_single_position_cap(self):
        """单仓位上限应受 max_single_pct 约束。"""
        sizer = PositionSizer(max_total_pct=80.0, max_single_pct=10.0)
        size = sizer.calculate(win_rate=0.6, avg_rr=3.0, regime="TRENDING", equity=10000.0)
        assert size <= 0.10

    def test_min_position_threshold(self):
        """低于 MIN_POSITION_PCT 时应返回 0。"""
        sizer = PositionSizer(max_total_pct=80.0, max_single_pct=80.0)
        # Kelly = (1.01*0.5 - 0.5)/1.01 ≈ 0.00495, *0.5(RANGING) ≈ 0.0025 < 0.01 → 返回 0
        size = sizer.calculate(win_rate=0.5, avg_rr=1.01, regime="RANGING", equity=10000.0)
        assert size == 0.0

    def test_invalid_params_return_zero(self):
        """无效参数应返回 0。"""
        sizer = PositionSizer(max_total_pct=80.0, max_single_pct=80.0)
        assert sizer.calculate(win_rate=0.0, avg_rr=2.0, regime="TRENDING", equity=10000.0) == 0.0
        assert sizer.calculate(win_rate=0.6, avg_rr=0.0, regime="TRENDING", equity=10000.0) == 0.0

    def test_calculate_size_value(self):
        """calculate_size_value 应返回正确的 USD 金额。"""
        sizer = PositionSizer(max_total_pct=80.0, max_single_pct=80.0)
        size_value = sizer.calculate_size_value(
            win_rate=0.6, avg_rr=3.0, regime="TRENDING", equity=10000.0
        )
        size_pct = sizer.calculate(
            win_rate=0.6, avg_rr=3.0, regime="TRENDING", equity=10000.0
        )
        assert size_value == pytest.approx(10000.0 * size_pct, rel=0.01)

    def test_regime_constants_match_arch(self):
        """REGIME_MULTIPLIER 应与 ARCH.md 第 7 节一致。"""
        assert REGIME_MULTIPLIER["TRENDING"] == 1.0
        assert REGIME_MULTIPLIER["RANGING"] == 0.5
        assert REGIME_MULTIPLIER["HIGH_VOLATILITY"] == 0.5
        assert REGIME_MULTIPLIER["UNKNOWN"] == 0.25


# ═══════════════════════════════════════════════════════════════
# exposure_monitor.py 测试
# ═══════════════════════════════════════════════════════════════

class TestExposureMonitor:
    """ExposureMonitor 持仓监控器测试。"""

    @pytest.mark.asyncio
    async def test_check_no_fetch_functions(self):
        """无 fetch 函数时应返回空快照。"""
        monitor = ExposureMonitor(max_exposure_pct=80.0)
        snapshot = await monitor.check()
        assert isinstance(snapshot, ExposureSnapshot)
        assert snapshot.total_exposure_usd == 0.0
        assert snapshot.total_equity_usd == 0.0
        assert snapshot.is_exceeded is False

    @pytest.mark.asyncio
    async def test_check_normal_exposure(self):
        """正常持仓应不触发超限。"""
        monitor = ExposureMonitor(max_exposure_pct=80.0)

        async def fetch_positions() -> list[dict]:
            return [{"symbol": "BTCUSDT", "side": "long", "stake_amount": 1000.0}]

        async def fetch_equity() -> float:
            return 10000.0

        snapshot = await monitor.check(fetch_positions, fetch_equity)
        assert snapshot.exposure_pct == pytest.approx(10.0, rel=0.1)
        assert snapshot.is_exceeded is False

    @pytest.mark.asyncio
    async def test_check_exceeded_exposure(self):
        """超限时应标记 is_exceeded=True。"""
        monitor = ExposureMonitor(max_exposure_pct=30.0)

        async def fetch_positions() -> list[dict]:
            return [{"symbol": "BTCUSDT", "side": "long", "stake_amount": 5000.0}]

        async def fetch_equity() -> float:
            return 10000.0

        snapshot = await monitor.check(fetch_positions, fetch_equity)
        assert snapshot.is_exceeded is True
        assert snapshot.exceed_by_pct > 0

    @pytest.mark.asyncio
    async def test_api_timeout_handled_gracefully(self):
        """API 超时应被优雅处理，不抛异常。"""
        import asyncio
        monitor = ExposureMonitor(max_exposure_pct=80.0)

        async def fetch_positions() -> list[dict]:
            raise asyncio.TimeoutError()

        async def fetch_equity() -> float:
            return 10000.0

        snapshot = await monitor.check(fetch_positions, fetch_equity)
        assert snapshot.total_exposure_usd == 0.0

    @pytest.mark.asyncio
    async def test_api_exception_handled_gracefully(self):
        """API 异常应被优雅处理，不抛异常。"""
        monitor = ExposureMonitor(max_exposure_pct=80.0)

        async def fetch_positions() -> list[dict]:
            raise ConnectionError("Connection refused")

        async def fetch_equity() -> float:
            return 10000.0

        snapshot = await monitor.check(fetch_positions, fetch_equity)
        assert snapshot.total_exposure_usd == 0.0

    @pytest.mark.asyncio
    async def test_multiple_positions_summed(self):
        """多个持仓应正确汇总。"""
        monitor = ExposureMonitor(max_exposure_pct=80.0)

        async def fetch_positions() -> list[dict]:
            return [
                {"symbol": "BTCUSDT", "side": "long", "stake_amount": 2000.0},
                {"symbol": "ETHUSDT", "side": "long", "stake_amount": 1000.0},
            ]

        async def fetch_equity() -> float:
            return 10000.0

        snapshot = await monitor.check(fetch_positions, fetch_equity)
        assert snapshot.total_exposure_usd == 3000.0
        assert len(snapshot.positions) == 2

    def test_parse_position(self):
        """_parse_position 应正确解析 Freqtrade 格式的持仓。"""
        raw = {
            "symbol": "BTCUSDT",
            "side": "long",
            "stake_amount": "1000.0",
            "unrealized_pnl": "50.0",
            "open_rate": "42000.0",
            "current_rate": "42500.0",
        }
        pos = ExposureMonitor._parse_position(raw)
        assert pos is not None
        assert pos.symbol == "BTCUSDT"
        assert pos.size_usd == 1000.0
        assert pos.unrealized_pnl_usd == 50.0

    def test_parse_position_missing_fields(self):
        """缺失字段的持仓应返回 None。"""
        raw = {"side": "long"}
        pos = ExposureMonitor._parse_position(raw)
        assert pos is None

    def test_correlated_pair_counting(self):
        """_count_correlated 应正确统计关联交易对。"""
        positions = [
            PositionInfo(symbol="BTCUSDT", side="long", size_usd=1000, unrealized_pnl_usd=0, entry_price=0, current_price=0),
            PositionInfo(symbol="BTCUSDT", side="short", size_usd=500, unrealized_pnl_usd=0, entry_price=0, current_price=0),
            PositionInfo(symbol="ETHUSDT", side="long", size_usd=800, unrealized_pnl_usd=0, entry_price=0, current_price=0),
        ]
        count = ExposureMonitor._count_correlated(positions)
        assert count == 1  # BTC 出现 2 次


# ═══════════════════════════════════════════════════════════════
# signal_arbiter.py 测试
# ═══════════════════════════════════════════════════════════════

class TestSignalArbiter:
    """SignalArbiter 信号仲裁器测试。"""

    def test_circuit_breaker_open_returns_flat(self, mock_breaker, mock_sizer, mock_drawdown):
        """熔断器 OPEN 时应返回 FLAT。"""
        mock_breaker.allow_open.return_value = False
        mock_breaker.state.value = "open"

        arbiter = SignalArbiter(
            circuit_breaker=mock_breaker,
            position_sizer=mock_sizer,
            drawdown_limit=mock_drawdown,
        )
        order = arbiter.arbitrate(
            ai_signal={"direction": "LONG", "confidence": 0.9},
        )
        assert order.action == "FLAT"
        assert "熔断器" in order.reasoning

    def test_drawdown_limit_blocks_position(self, mock_breaker, mock_sizer, mock_drawdown):
        """回撤限制中应返回 FLAT。"""
        mock_breaker.allow_open.return_value = True
        mock_drawdown.can_open_position.return_value = False
        mock_drawdown.check_limits.return_value = {"level": "DAILY"}

        arbiter = SignalArbiter(
            circuit_breaker=mock_breaker,
            position_sizer=mock_sizer,
            drawdown_limit=mock_drawdown,
        )
        order = arbiter.arbitrate(
            ai_signal={"direction": "LONG", "confidence": 0.9},
        )
        assert order.action == "FLAT"
        assert "回撤限制" in order.reasoning

    def test_ai_high_confidence_used(self, mock_breaker, mock_sizer, mock_drawdown):
        """AI 置信度 > 0.8 且方向非 FLAT 应使用 AI 信号。"""
        mock_breaker.allow_open.return_value = True
        mock_drawdown.can_open_position.return_value = True
        mock_sizer.calculate.return_value = 0.08

        arbiter = SignalArbiter(
            circuit_breaker=mock_breaker,
            position_sizer=mock_sizer,
            drawdown_limit=mock_drawdown,
        )
        order = arbiter.arbitrate(
            ai_signal={
                "direction": "LONG",
                "confidence": 0.85,
                "entry": 42000.0,
                "sl": 41500.0,
                "tp": 43500.0,
                "regime": "TRENDING",
                "reasoning": "EMA 多头排列",
            },
            regime="TRENDING",
        )
        assert order.action == "LONG"
        assert order.source == "ai_signal"
        assert order.entry == 42000.0

    def test_ai_low_confidence_falls_to_freqtrade(self, mock_breaker, mock_sizer, mock_drawdown):
        """AI 置信度低于阈值应使用 Freqtrade 信号。"""
        mock_breaker.allow_open.return_value = True
        mock_drawdown.can_open_position.return_value = True

        arbiter = SignalArbiter(
            circuit_breaker=mock_breaker,
            position_sizer=mock_sizer,
            drawdown_limit=mock_drawdown,
        )
        order = arbiter.arbitrate(
            ai_signal={"direction": "LONG", "confidence": 0.5},
            freqtrade_signal={"symbol": "BTCUSDT", "action": "SHORT", "entry": 41000.0},
        )
        assert order.action == "SHORT"
        assert order.source == "freqtrade_native"

    def test_no_ai_signal_uses_freqtrade(self, mock_breaker, mock_sizer, mock_drawdown):
        """无 AI 信号时应使用 Freqtrade 信号。"""
        mock_breaker.allow_open.return_value = True
        mock_drawdown.can_open_position.return_value = True

        arbiter = SignalArbiter(
            circuit_breaker=mock_breaker,
            position_sizer=mock_sizer,
            drawdown_limit=mock_drawdown,
        )
        order = arbiter.arbitrate(
            ai_signal=None,
            freqtrade_signal={"symbol": "BTCUSDT", "action": "LONG"},
        )
        assert order.action == "LONG"
        assert order.source == "freqtrade_native"

    def test_no_signals_at_all_returns_flat(self, mock_breaker, mock_sizer, mock_drawdown):
        """没有任何信号时应返回 FLAT。"""
        mock_breaker.allow_open.return_value = True
        mock_drawdown.can_open_position.return_value = True

        arbiter = SignalArbiter(
            circuit_breaker=mock_breaker,
            position_sizer=mock_sizer,
            drawdown_limit=mock_drawdown,
        )
        order = arbiter.arbitrate(ai_signal=None)
        assert order.action == "FLAT"

    def test_regime_mismatch_falls_to_freqtrade(self, mock_breaker, mock_sizer, mock_drawdown):
        """制度不匹配时应使用 Freqtrade 信号。"""
        mock_breaker.allow_open.return_value = True
        mock_drawdown.can_open_position.return_value = True

        arbiter = SignalArbiter(
            circuit_breaker=mock_breaker,
            position_sizer=mock_sizer,
            drawdown_limit=mock_drawdown,
            require_regime_match=True,
        )
        order = arbiter.arbitrate(
            ai_signal={
                "direction": "LONG",
                "confidence": 0.85,
                "regime": "TRENDING",
            },
            freqtrade_signal={"symbol": "BTCUSDT", "action": "SHORT"},
            regime="RANGING",
        )
        assert order.action == "SHORT"
        assert order.source == "freqtrade_native"

    def test_arbitrated_order_has_audit_id(self):
        """ArbitratedOrder 应自动生成 audit_id。"""
        order = ArbitratedOrder(
            symbol="BTCUSDT",
            ts=1700000000000,
            action="LONG",
            size_pct=0.08,
        )
        assert order.audit_id
        assert len(order.audit_id) == 36

    def test_to_stream_message_format(self):
        """to_stream_message 应输出标准 trade_order 格式。"""
        order = ArbitratedOrder(
            symbol="BTCUSDT",
            ts=1700000000000,
            action="LONG",
            size_pct=0.08,
            entry=42000.0,
            sl=41500.0,
            tp=43500.0,
            source="ai_signal",
            breaker_state="CLOSED",
            audit_id="test-uuid",
        )
        msg = order.to_stream_message()
        assert msg["symbol"] == "BTCUSDT"
        assert msg["action"] == "LONG"
        assert msg["size_pct"] == 0.08
        assert msg["audit_id"] == "test-uuid"

    def test_check_and_publish_returns_stream_message(self, mock_breaker, mock_sizer, mock_drawdown):
        """check_and_publish 应返回可直接发布的字典。"""
        mock_breaker.allow_open.return_value = True
        mock_drawdown.can_open_position.return_value = True
        mock_sizer.calculate.return_value = 0.08

        arbiter = SignalArbiter(
            circuit_breaker=mock_breaker,
            position_sizer=mock_sizer,
            drawdown_limit=mock_drawdown,
        )
        msg = arbiter.check_and_publish(
            ai_signal={
                "direction": "LONG",
                "confidence": 0.85,
                "entry": 42000.0,
                "sl": 41500.0,
                "tp": 43500.0,
                "regime": "TRENDING",
                "reasoning": "看多",
            },
            regime="TRENDING",
        )
        assert isinstance(msg, dict)
        assert msg["action"] == "LONG"
        assert "audit_id" in msg
        assert msg["source"] == "ai_signal"

    def test_estimate_rr_long(self):
        """估算 LONG 方向盈亏比。"""
        rr = SignalArbiter._estimate_rr(entry=100.0, sl=95.0, tp=110.0, direction="LONG")
        assert rr == pytest.approx(2.0, rel=0.01)

    def test_estimate_rr_short(self):
        """估算 SHORT 方向盈亏比。"""
        rr = SignalArbiter._estimate_rr(entry=100.0, sl=105.0, tp=90.0, direction="SHORT")
        assert rr == pytest.approx(2.0, rel=0.01)

    def test_estimate_rr_missing_data(self):
        """缺少价格数据时应返回默认值 2.0。"""
        rr = SignalArbiter._estimate_rr(entry=None, sl=None, tp=None, direction="LONG")
        assert rr == 2.0


# ═══════════════════════════════════════════════════════════════
# circuit_breaker.py 集成测试
# ═══════════════════════════════════════════════════════════════

class TestCircuitBreakerIntegration:
    """CircuitBreaker 与 SignalArbiter 集成测试。"""

    def test_circuit_breaker_and_arbiter_integration(self):
        """熔断器与仲裁器集成：OPEN 时所有信号变为 FLAT。"""
        from risk_guardian.circuit_breaker import CircuitBreaker

        breaker = CircuitBreaker(max_daily_dd=5.0, equity_floor=0.0, max_consec_loss=5)
        breaker._trip("测试熔断")
        assert breaker.allow_open() is False

        arbiter = SignalArbiter(circuit_breaker=breaker)
        order = arbiter.arbitrate(
            ai_signal={"direction": "LONG", "confidence": 0.99},
        )
        assert order.action == "FLAT"
        assert "熔断器" in order.reasoning


# ═══════════════════════════════════════════════════════════════
# circuit_breaker.py 基础测试
# ═══════════════════════════════════════════════════════════════

class TestCircuitBreakerBase:
    """CircuitBreaker 基础功能测试。"""

    def test_initial_state_closed(self):
        from risk_guardian.circuit_breaker import CircuitBreaker, BreakerState
        cb = CircuitBreaker()
        assert cb.state == BreakerState.CLOSED
        assert cb.allow_open() is True

    def test_update_equity_triggers_drawdown(self):
        from risk_guardian.circuit_breaker import CircuitBreaker, BreakerState
        cb = CircuitBreaker(max_daily_dd=5.0)
        cb.update_equity(10000.0)
        cb.update_equity(9300.0)
        assert cb.state == BreakerState.OPEN

    def test_equity_floor_triggers(self):
        from risk_guardian.circuit_breaker import CircuitBreaker, BreakerState
        cb = CircuitBreaker(equity_floor=1000.0)
        cb.update_equity(500.0)
        assert cb.state == BreakerState.OPEN

    def test_consecutive_losses_triggers(self):
        from risk_guardian.circuit_breaker import CircuitBreaker, BreakerState
        cb = CircuitBreaker(max_consec_loss=3)
        cb.record_trade(-10)
        cb.record_trade(-20)
        cb.record_trade(-30)
        assert cb.state == BreakerState.OPEN

    def test_winning_trade_resets_consecutive_losses(self):
        from risk_guardian.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(max_consec_loss=3)
        cb.record_trade(-10)
        cb.record_trade(-20)
        cb.record_trade(50)
        assert cb._consec == 0

    def test_reset_returns_to_closed(self):
        from risk_guardian.circuit_breaker import CircuitBreaker, BreakerState
        cb = CircuitBreaker()
        cb._trip("test")
        cb.reset()
        assert cb.state == BreakerState.CLOSED


# ═══════════════════════════════════════════════════════════════
# AiSignalStrategy 关键逻辑测试
# ═══════════════════════════════════════════════════════════════

class TestAiSignalStrategyLogic:
    """AiSignalStrategy 核心逻辑测试（不依赖 Freqtrade 框架）。"""

    def test_load_signal_from_payload(self):
        """load_signal_from_payload 应正确解析 JSON payload。"""
        from freqtrade_strategies.AiSignalStrategy import load_signal_from_payload

        payload = json.dumps({"symbol": "BTCUSDT", "action": "LONG", "size_pct": 0.08})
        result = load_signal_from_payload(payload)
        assert result["action"] == "LONG"
        assert result["size_pct"] == 0.08

    def test_load_signal_from_invalid_payload(self):
        """无效 JSON payload 应返回 None。"""
        from freqtrade_strategies.AiSignalStrategy import load_signal_from_payload

        result = load_signal_from_payload("{invalid}")
        assert result is None

    def test_load_signal_from_empty_payload(self):
        """空 payload 应返回 None。"""
        from freqtrade_strategies.AiSignalStrategy import load_signal_from_payload

        result = load_signal_from_payload("")
        assert result is None

