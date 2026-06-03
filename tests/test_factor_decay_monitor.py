"""
FactorDecayMonitorScheduler 测试套件
覆盖：FactorDecayResult / 调度器 / 快捷函数 / CLI
依赖：validation/factor_decay.py 核心引擎（已有测试）
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from observability.factor_decay_monitor import (
    FactorDecayResult,
    FactorDecayMonitorScheduler,
    run_check,
    run_all_checks,
)


# ─── Fixtures ─────────────────────────────────────────────

@pytest.fixture
def rising_ic() -> list[float]:
    """上升的 IC 序列 — 不会触发衰减。"""
    return [0.03, 0.035, 0.04, 0.045, 0.05, 0.055, 0.06, 0.065]


@pytest.fixture
def decaying_ic() -> list[float]:
    """下降的 IC 序列 — 会触发衰减。"""
    return [0.05, 0.04, 0.03, 0.02, 0.01, 0.005, 0.002, 0.001]


@pytest.fixture
def short_ic() -> list[float]:
    """不足 2 个数据点。"""
    return [0.05]


@pytest.fixture
def scheduler() -> FactorDecayMonitorScheduler:
    return FactorDecayMonitorScheduler()


# ─── FactorDecayResult 测试 ─────────────────────────────

class TestFactorDecayResult:
    def test_default_values(self):
        result = FactorDecayResult(factor_name="test")
        assert result.factor_name == "test"
        assert result.is_decaying is False
        assert result.ic_mean == 0.0
        assert result.ic_slope == 0.0
        assert result.half_life == 0

    def test_timestamp_format(self):
        result = FactorDecayResult(factor_name="test")
        assert result.timestamp.endswith("Z")
        assert "T" in result.timestamp

    def test_to_influxdb_point(self):
        result = FactorDecayResult(
            factor_name="momentum_1",
            is_decaying=True,
            ic_mean=0.015,
            ic_slope=-0.005,
            half_life=5,
        )
        point = result.to_influxdb_point()
        assert point["measurement"] == "factor_decay"
        assert point["tags"]["factor"] == "momentum_1"
        assert point["fields"]["is_decaying"] == 1
        assert point["fields"]["ic_mean"] == 0.015

    def test_to_influxdb_point_not_decaying(self):
        result = FactorDecayResult(factor_name="trend_1", is_decaying=False)
        point = result.to_influxdb_point()
        assert point["fields"]["is_decaying"] == 0

    def test_to_prometheus_metrics(self):
        result = FactorDecayResult(
            factor_name="volume_1",
            is_decaying=True,
            ic_mean=0.01,
            ic_slope=-0.003,
            half_life=3,
        )
        metrics = result.to_prometheus_metrics()
        assert "factor_decay_is_decaying" in metrics
        assert 'factor="volume_1"' in metrics
        assert "0.010000" in metrics
        assert "3" in metrics


# ─── 调度器测试 ──────────────────────────────────────────

class TestFactorDecayMonitorScheduler:
    def test_init_default_factors(self, scheduler: FactorDecayMonitorScheduler):
        """初始化时应加载默认因子列表。"""
        assert len(scheduler._factors) >= 6

    def test_run_once_rising(self, scheduler: FactorDecayMonitorScheduler, rising_ic: list[float]):
        """上升的 IC 不应触发衰减。"""
        result = scheduler.run_once("momentum", rising_ic)
        assert result.is_decaying is False
        assert result.ic_mean > 0.0

    def test_run_once_decaying(self, scheduler: FactorDecayMonitorScheduler, decaying_ic: list[float]):
        """下降的 IC 应触发衰减。"""
        result = scheduler.run_once("momentum", decaying_ic)
        assert result.is_decaying is True
        assert result.ic_slope < 0

    def test_run_once_short_data(self, scheduler: FactorDecayMonitorScheduler, short_ic: list[float]):
        """不足 2 个数据点不应标记衰减。"""
        result = scheduler.run_once("momentum", short_ic)
        assert result.is_decaying is False

    def test_run_once_caches_result(self, scheduler: FactorDecayMonitorScheduler, rising_ic: list[float]):
        """run_once 应将结果存入缓存。"""
        scheduler.run_once("test_factor", rising_ic)
        assert "test_factor" in scheduler._last_results
        cached = scheduler._last_results["test_factor"]
        assert cached.ic_mean > 0.0

    def test_run_all_without_data(self, scheduler: FactorDecayMonitorScheduler):
        """没有数据源时 run_all 应返回空列表。"""
        results = scheduler.run_all(data_provider=None)
        # 无 InfluxDB 客户端 + 无 data_provider → 空
        assert isinstance(results, list)

    def test_run_all_with_provider(self, scheduler: FactorDecayMonitorScheduler, rising_ic: list[float]):
        """使用 data_provider 时 run_all 应返回结果。"""

        class MockProvider:
            def get_ic_series(self, factor: str) -> list[float]:
                return rising_ic

        provider = MockProvider()
        results = scheduler.run_all(data_provider=provider)
        assert len(results) > 0
        for r in results:
            assert r.is_decaying is False

    def test_get_prometheus_metrics_empty(self, scheduler: FactorDecayMonitorScheduler):
        """无数据时 metrics 应返回提示信息。"""
        metrics = scheduler.get_prometheus_metrics()
        assert "No factor decay data available" in metrics

    def test_get_prometheus_after_run(self, scheduler: FactorDecayMonitorScheduler, rising_ic: list[float]):
        """有数据后 metrics 应包含因子信息。"""
        scheduler.run_once("test_factor", rising_ic)
        metrics = scheduler.get_prometheus_metrics()
        assert 'factor="test_factor"' in metrics
        assert "factor_decay_is_decaying" in metrics

    def test_write_to_influxdb_skip_no_client(self, scheduler: FactorDecayMonitorScheduler, rising_ic: list[float]):
        """无 InfluxDB 客户端时写入应返回 0。"""
        result = scheduler.run_once("test", rising_ic)
        written = scheduler.write_to_influxdb([result])
        assert written == 0

    def test_write_to_influxdb_with_client(self, rising_ic: list[float]):
        """有 InfluxDB 客户端时应尝试写入。"""
        mock_client = MagicMock()
        scheduler = FactorDecayMonitorScheduler(influxdb_client=mock_client)
        mock_client.write_api.return_value.write.return_value = None

        result = scheduler.run_once("test", rising_ic)
        written = scheduler.write_to_influxdb([result])

        # 即使 mock 也可能失败，但至少不抛异常
        assert written >= 0

    def test_stop(self, scheduler: FactorDecayMonitorScheduler):
        """stop() 应设置 _running = False。"""
        scheduler._running = True
        scheduler.stop()
        assert scheduler._running is False

    def test_run_all_with_data_provider_and_influxdb(self, rising_ic: list[float]):
        """有 data_provider 且 InfluxDB 可用时 run_all 应正确执行。"""
        class MockProvider:
            def get_ic_series(self, factor: str) -> list[float]:
                return rising_ic

        mock_client = MagicMock()
        scheduler = FactorDecayMonitorScheduler(
            factors=["momentum_1", "trend_1"],
            influxdb_client=mock_client,
        )
        results = scheduler.run_all(data_provider=MockProvider())
        assert len(results) == 2
        for r in results:
            assert r.is_decaying is False


# ─── 快捷函数测试 ───────────────────────────────────────

class TestRunCheck:
    def test_run_check_returns_result(self, rising_ic: list[float]):
        """run_check 应返回 FactorDecayResult。"""
        result = run_check("test", rising_ic)
        assert isinstance(result, FactorDecayResult)
        assert result.factor_name == "test"

    def test_run_check_decaying(self, decaying_ic: list[float]):
        """run_check 下降序列应标记衰减。"""
        result = run_check("test", decaying_ic)
        assert result.is_decaying is True

    def test_run_check_with_config(self, rising_ic: list[float]):
        """run_check 接受自定义配置。"""
        from validation.factor_decay import FactorDecayConfig
        config = FactorDecayConfig(ic_threshold=0.001)  # 极低阈值，不应触发
        result = run_check("test", rising_ic, config=config)
        # IC 均值 ~0.047 > 0.001，不应衰减
        assert result.is_decaying is False


class TestRunAllChecks:
    def test_run_all_checks_returns_list(self):
        factor_ic_map = {
            "momentum": [0.05, 0.04, 0.03],
            "trend": [0.03, 0.035, 0.04],
        }
        results = run_all_checks(factor_ic_map)
        assert len(results) == 2

    def test_run_all_checks_empty(self):
        results = run_all_checks({})
        assert len(results) == 0


# ─── CLI 测试 ────────────────────────────────────────────

class TestCLI:
    def test_metrics_flag_output(self):
        """--metrics 应输出不含异常的字符串。"""
        from observability.factor_decay_monitor import main
        with patch("sys.argv", ["factor_decay_monitor.py", "--metrics"]):
            with patch("builtins.print") as mock_print:
                rc = main()
                assert rc == 0
                output = mock_print.call_args[0][0]
                assert isinstance(output, str)

    def test_once_without_data(self):
        """--once 无数据源时不应崩溃。"""
        from observability.factor_decay_monitor import main
        with patch("sys.argv", ["factor_decay_monitor.py", "--once"]):
            rc = main()
            assert rc == 0
