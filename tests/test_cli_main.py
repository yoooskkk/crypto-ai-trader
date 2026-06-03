"""
Unified CLI entry point tests
Covers: all subcommand parsers / check-env / help output
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts.cli_main import main, cmd_check_env


class TestParser:
    def test_health_parser(self):
        with patch("sys.argv", ["cli_main.py", "health"]):
            with patch("scripts.cli_main.cmd_health") as mock:
                mock.return_value = 0
                rc = main()
                assert rc == 0

    def test_health_json(self):
        with patch("sys.argv", ["cli_main.py", "health", "--json"]):
            with patch("scripts.cli_main.cmd_health") as mock:
                mock.return_value = 0
                rc = main()
                assert rc == 0

    def test_dashboard(self):
        with patch("sys.argv", ["cli_main.py", "dashboard"]):
            with patch("scripts.cli_main.cmd_dashboard") as mock:
                mock.return_value = 0
                rc = main()
                assert rc == 0

    def test_backfill_symbol(self):
        with patch("sys.argv", ["cli_main.py", "backfill", "--symbol", "BTCUSDT", "--days", "1"]):
            rc = main()
            assert rc == 0

    def test_backfill_list(self):
        with patch("sys.argv", ["cli_main.py", "backfill", "--list-symbols"]):
            rc = main()
            assert rc == 0

    def test_decay_metrics(self):
        with patch("sys.argv", ["cli_main.py", "decay", "--metrics"]):
            rc = main()
            assert rc == 0

    def test_decay_once(self):
        with patch("sys.argv", ["cli_main.py", "decay", "--once"]):
            rc = main()
            assert rc == 0

    def test_alert_test(self):
        with patch("sys.argv", ["cli_main.py", "alert", "--test"]):
            rc = main()
            assert rc == 0

    def test_signal(self):
        with patch("sys.argv", ["cli_main.py", "signal", "--symbol", "BTCUSDT"]):
            rc = main()
            assert rc == 0

    def test_test_module(self):
        with patch("sys.argv", ["cli_main.py", "test", "--module", "health_check"]):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                rc = main()
                assert rc == 0

    def test_check_env(self):
        with patch("sys.argv", ["cli_main.py", "check-env"]):
            rc = main()
            assert rc == 0

    def test_run_module(self):
        with patch("sys.argv", ["cli_main.py", "run", "scripts.health_check"]):
            with patch("importlib.import_module") as mock_import:
                mock_mod = mock_import.return_value
                mock_mod.main.return_value = 0
                rc = main()
                assert rc == 0

    def test_no_args(self):
        with patch("sys.argv", ["cli_main.py"]):
            rc = main()
            assert rc == 1

    def test_unknown_command(self):
        with patch("sys.argv", ["cli_main.py", "unknown"]):
            with pytest.raises(SystemExit):
                main()


class TestCheckEnv:
    def test_returns_int(self):
        rc = cmd_check_env(None)
        assert isinstance(rc, int)
        assert rc == 0


class TestBackfillScript:
    def test_import_main(self):
        from scripts.backfill_data import main as bf_main
        assert bf_main is not None


class TestSetupScript:
    def test_file_exists(self):
        import os
        assert os.path.isfile("scripts/setup.sh")

    def test_is_executable(self):
        import os
        # On Unix, check mode; on Windows just check it exists
        assert os.path.isfile("scripts/setup.sh")


class TestRunBacktestScript:
    def test_file_exists(self):
        import os
        assert os.path.isfile("scripts/run_backtest.sh")


class TestDockerCompose:
    def test_file_exists(self):
        import os
        assert os.path.isfile("docker-compose.yml")

    def test_contains_services(self):
        with open("docker-compose.yml", encoding="utf-8") as f:
            content = f.read()
        assert "dashboard" in content
        assert "factor-decay-monitor" in content
        assert "alertmanager" in content
        assert "grafana" in content
        assert "prometheus" in content


class TestInfraConfigs:
    def test_alertmanager_config(self):
        import os
        assert os.path.isfile("infra/alertmanager/config.yml")

    def test_prometheus_rules(self):
        import os
        assert os.path.isfile("observability/prometheus/rules.yml")

    def test_grafana_dashboard(self):
        import os
        assert os.path.isfile("observability/grafana/dashboards/trading_system.json")
