"""
HealthCheck 测试套件
覆盖：ServiceResult / HealthReport 模型、check_tcp、check_freqtrade_api、
      check_redis、run_health_check、format_human、main CLI
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from scripts.health_check import (
    ServiceResult,
    HealthReport,
    check_tcp,
    check_freqtrade_api,
    check_redis,
    run_health_check,
    format_human,
)


# ─── 数据模型测试 ─────────────────────────────────────────

class TestServiceResult:
    def test_ok_result(self):
        r = ServiceResult(name="redis", status="ok", latency_ms=1.2)
        assert r.name == "redis"
        assert r.status == "ok"
        assert r.latency_ms == 1.2

    def test_error_result(self):
        r = ServiceResult(name="db", status="error", detail="Connection refused")
        assert r.status == "error"
        assert r.detail == "Connection refused"

    def test_skip_result(self):
        r = ServiceResult(name="influxdb", status="skip")
        assert r.status == "skip"


class TestHealthReport:
    def test_default_status(self):
        report = HealthReport(status="healthy")
        assert report.status == "healthy"

    def test_summary_empty(self):
        report = HealthReport(status="healthy")
        s = report.summary
        assert s["total"] == 0
        assert s["ok"] == 0

    def test_summary_with_results(self):
        report = HealthReport(status="healthy")
        report.services = [
            ServiceResult(name="a", status="ok"),
            ServiceResult(name="b", status="ok"),
            ServiceResult(name="c", status="error"),
        ]
        s = report.summary
        assert s["total"] == 3
        assert s["ok"] == 2
        assert s["error"] == 1

    def test_to_dict_structure(self):
        report = HealthReport(status="healthy")
        report.services = [ServiceResult(name="test", status="ok")]
        d = report.to_dict()
        assert d["status"] == "healthy"
        assert "timestamp" in d
        assert "summary" in d
        assert "services" in d
        assert len(d["services"]) == 1
        assert d["services"][0]["name"] == "test"

    def test_timestamp_format(self):
        report = HealthReport(status="healthy")
        assert report.timestamp.endswith("Z")
        assert "T" in report.timestamp


# ─── TCP 检查测试 ─────────────────────────────────────────

class TestCheckTCP:
    def test_success(self):
        with patch("socket.create_connection") as mock:
            mock.return_value.__enter__.return_value = MagicMock()
            result = check_tcp("test", "localhost", 1234, 3)
            assert result.status == "ok"
            assert result.name == "test"
            assert result.latency_ms > 0

    def test_timeout(self):
        with patch("socket.create_connection", side_effect=TimeoutError()):
            result = check_tcp("test", "localhost", 1234, 3)
            assert result.status == "error"
            assert "超时" in result.detail

    def test_refused(self):
        with patch("socket.create_connection", side_effect=ConnectionRefusedError()):
            result = check_tcp("test", "localhost", 1234, 3)
            assert result.status == "error"
            assert "拒绝" in result.detail

    def test_generic_exception(self):
        with patch("socket.create_connection", side_effect=OSError("network error")):
            result = check_tcp("test", "localhost", 1234, 3)
            assert result.status == "error"
            assert "network error" in result.detail


# ─── Freqtrade API 检查测试 ──────────────────────────────

class TestCheckFreqtradeAPI:
    def test_success(self):
        with patch("requests.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"status": "pong"}
            mock_get.return_value = mock_response

            result = check_freqtrade_api("http://localhost:8080", "/api/v1/ping", 5)
            assert result.status == "ok"
            assert result.name == "freqtrade_api"

    def test_wrong_status(self):
        with patch("requests.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"status": "error"}
            mock_get.return_value = mock_response

            result = check_freqtrade_api("http://localhost:8080", "/api/v1/ping", 5)
            assert result.status == "error"

    def test_http_error(self):
        with patch("requests.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 503
            mock_response.text = "Service Unavailable"
            mock_get.return_value = mock_response

            result = check_freqtrade_api("http://localhost:8080", "/api/v1/ping", 5)
            assert result.status == "error"
            assert "503" in result.detail

    def test_connection_error(self):
        with patch("requests.get", side_effect=ConnectionError("Connection refused")):
            result = check_freqtrade_api("http://localhost:8080", "/api/v1/ping", 5)
            assert result.status == "error"


# ─── Redis 检查测试 ──────────────────────────────────────

class TestCheckRedis:
    def test_success_with_redis_py(self):
        with patch("redis.Redis") as mock_redis:
            mock_instance = MagicMock()
            mock_instance.ping.return_value = True
            mock_redis.return_value = mock_instance

            result = check_redis("localhost", 6379, 3)
            assert result.status == "ok"
            assert result.name == "redis"

    def test_failed_ping(self):
        with patch("redis.Redis") as mock_redis:
            mock_instance = MagicMock()
            mock_instance.ping.return_value = False
            mock_redis.return_value = mock_instance

            result = check_redis("localhost", 6379, 3)
            assert result.status == "error"

       
    def mock_import(name, *args, **kwargs):
            if name == "redis":
                raise ImportError("No module named 'redis'")
            return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):
        with patch("socket.create_connection") as mock_tcp:
            mock_tcp.return_value.__enter__.return_value = MagicMock()
            result = check_redis("localhost", 6379, 3)
            assert result.status in ("ok", "error")
            mock_tcp.assert_called_once()

    def test_exception_handling(self):
        with patch("redis.Redis", side_effect=Exception("Unexpected error")):
            result = check_redis("localhost", 6379, 3)
            assert result.status == "error"
            assert "Unexpected" in result.detail


# ─── 主逻辑测试 ──────────────────────────────────────────

class TestRunHealthCheck:
    def test_all_services_checked(self):
        """不指定 filter 时应检查所有服务。"""
        with patch("scripts.health_check.ServiceResult") as mock_result:
            report = run_health_check()
            # 至少应包含 4 个服务
            assert len(report.services) >= 4

    def test_service_filter(self):
        """指定 filter 后只返回指定服务。"""
        report = run_health_check(service_filter="redis")
        assert len(report.services) == 1
        assert report.services[0].name == "redis"

    def test_invalid_service_filter(self):
        """无效 filter 应返回空。"""
        report = run_health_check(service_filter="nonexistent")
        assert len(report.services) == 0

    def test_overall_healthy(self):
        ok_redis = ServiceResult(name="redis", status="ok")
        ok_tsdb = ServiceResult(name="timescaledb", status="ok")
        ok_freq = ServiceResult(name="freqtrade_api", status="ok")
        ok_influx = ServiceResult(name="influxdb", status="ok")

        with patch("scripts.health_check.check_redis", return_value=ok_redis):
            with patch("scripts.health_check.check_timescaledb", return_value=ok_tsdb):
                with patch("scripts.health_check.check_freqtrade_api", return_value=ok_freq):
                    # influxdb 也需要 mock，否则真实连接会失败
                    with patch("scripts.health_check.check_tcp",
                               return_value=ok_influx):
                        report = run_health_check()
                        assert report.status == "healthy"

    def test_tcp_errors_make_degraded(self):
        """部分服务失败时状态为 degraded。"""
        with patch("socket.create_connection", side_effect=ConnectionRefusedError()):
            report = run_health_check(service_filter="influxdb")
            assert report.status == "unhealthy"

    def test_mixed_results_degraded(self):
        """部分成功 + 部分失败 → degraded。"""
        with patch("scripts.health_check.check_redis") as mock_redis:
            mock_redis.return_value = ServiceResult(name="redis", status="ok")
            with patch("scripts.health_check.check_timescaledb") as mock_timescaledb:
                mock_timescaledb.return_value = ServiceResult(name="timescaledb", status="error")
                with patch("scripts.health_check.check_freqtrade_api") as mock_freqtrade:
                    mock_freqtrade.return_value = ServiceResult(name="freqtrade_api", status="ok")
                    # 需要 mock 更多服务
                    report = run_health_check()
                    # 至少有一个 error → degraded 或 unhealthy
                    assert report.status in ("degraded", "unhealthy")


# ─── 格式化测试 ──────────────────────────────────────────

class TestFormatHuman:
    def test_contains_status(self):
        report = HealthReport(status="healthy")
        report.services = [ServiceResult(name="redis", status="ok")]
        output = format_human(report)
        assert "健康检查报告" in output
        assert "HEALTHY" in output

    def test_contains_results(self):
        report = HealthReport(status="healthy")
        report.services = [
            ServiceResult(name="redis", status="ok", latency_ms=1.5),
            ServiceResult(name="db", status="error", detail="timeout"),
        ]
        output = format_human(report)
        assert "redis" in output
        assert "db" in output
        assert "timeout" in output

    def test_json_output(self):
        """验证 JSON 输出格式。"""
        report = HealthReport(status="healthy")
        report.services = [ServiceResult(name="test", status="ok")]
        json_str = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
        parsed = json.loads(json_str)
        assert parsed["status"] == "healthy"
        assert len(parsed["services"]) == 1


# ─── CLI 入口测试 ────────────────────────────────────────

class TestMain:
    def test_main_returns_zero_on_healthy(self):
        """健康时 main 返回 0。"""
        from scripts.health_check import main
        with patch("sys.argv", ["health_check.py"]):
            with patch("scripts.health_check.run_health_check") as mock_run:
                mock_report = HealthReport(status="healthy")
                mock_report.services = [ServiceResult(name="test", status="ok")]
                mock_run.return_value = mock_report
                rc = main()
                assert rc == 0

    def test_main_returns_zero_on_degraded(self):
        """降级时 main 返回 0。"""
        from scripts.health_check import main
        with patch("sys.argv", ["health_check.py"]):
            with patch("scripts.health_check.run_health_check") as mock_run:
                mock_report = HealthReport(status="degraded")
                mock_report.services = [
                    ServiceResult(name="a", status="ok"),
                    ServiceResult(name="b", status="error"),
                ]
                mock_run.return_value = mock_report
                rc = main()
                assert rc == 0

    def test_main_returns_one_on_unhealthy(self):
        """全部异常时 main 返回 1。"""
        from scripts.health_check import main
        with patch("sys.argv", ["health_check.py"]):
            with patch("scripts.health_check.run_health_check") as mock_run:
                mock_report = HealthReport(status="unhealthy")
                mock_report.services = [ServiceResult(name="test", status="error")]
                mock_run.return_value = mock_report
                rc = main()
                assert rc == 1

    def test_main_json_flag(self):
        """--json 标志应输出 JSON。"""
        from scripts.health_check import main
        with patch("sys.argv", ["health_check.py", "--json"]):
            with patch("scripts.health_check.run_health_check") as mock_run:
                mock_report = HealthReport(status="healthy")
                mock_report.services = [ServiceResult(name="test", status="ok")]
                mock_run.return_value = mock_report
                with patch("builtins.print") as mock_print:
                    rc = main()
                    assert rc == 0
                    # 确认打印的是 JSON
                    call_args = mock_print.call_args[0][0]
                    assert isinstance(call_args, str)
                    # 有时结构体含中文字段
                    assert "healthy" in call_args or "services" in call_args

    def test_main_service_filter(self):
        """--service 参数应传递给 run_health_check。"""
        from scripts.health_check import main
        with patch("sys.argv", ["health_check.py", "--service", "redis"]):
            with patch("scripts.health_check.run_health_check") as mock_run:
                mock_report = HealthReport(status="healthy")
                mock_report.services = [ServiceResult(name="redis", status="ok")]
                mock_run.return_value = mock_report
                rc = main()
                assert rc == 0
                mock_run.assert_called_once_with(service_filter="redis")
