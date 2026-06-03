"""
统一 CLI 入口 — crypto-ai-trader
将所有模块的命令行接口汇总到一个入口点。

用法:
    python -m scripts.cli_main health --service redis
    python -m scripts.cli_main dashboard
    python -m scripts.cli_main backfill --symbol BTCUSDT --days 7
    python -m scripts.cli_main decay --once
    python -m scripts.cli_main alert --test
    python -m scripts.cli_main signal --symbol BTCUSDT
    python -m scripts.cli_main test --module risk_guardian
    python -m scripts.cli_main check-env
"""
from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from typing import NoReturn


def cmd_health(args: argparse.Namespace) -> int:
    """运行健康检查。"""
    from scripts.health_check import main as health_main
    sys.argv = ["health_check.py"]
    if args.services:
        for s in args.services:
            sys.argv.extend(["--service", s])
    if args.json:
        sys.argv.append("--json")
    return health_main()


def cmd_dashboard(args: argparse.Namespace) -> int:
    """启动 Web 仪表板。"""
    from ui.dashboard.app import main as dash_main
    return dash_main()


def cmd_backfill(args: argparse.Namespace) -> int:
    """数据回填。"""
    from scripts.backfill_data import main as bf_main
    sys.argv = ["backfill_data.py"]
    if args.symbol:
        sys.argv.extend(["--symbol", args.symbol])
    if args.all_major:
        sys.argv.append("--all-major")
    if args.list_symbols:
        sys.argv.append("--list-symbols")
    if args.interval:
        sys.argv.extend(["--interval", args.interval])
    if args.start:
        sys.argv.extend(["--start", args.start])
    if args.end:
        sys.argv.extend(["--end", args.end])
    if args.days:
        sys.argv.extend(["--days", str(args.days)])
    if args.exchange:
        sys.argv.extend(["--exchange", args.exchange])
    if args.concurrency:
        sys.argv.extend(["--concurrency", str(args.concurrency)])
    return bf_main()


def cmd_decay(args: argparse.Namespace) -> int:
    """因子衰减检查。"""
    from observability.factor_decay_monitor import main as decay_main
    sys.argv = ["factor_decay_monitor.py"]
    if args.once:
        sys.argv.append("--once")
    if args.factor:
        sys.argv.extend(["--factor", args.factor])
    if args.metrics:
        sys.argv.append("--metrics")
    return decay_main()


def cmd_alert(args: argparse.Namespace) -> int:
    """测试告警系统。"""
    from observability.alert_manager import alert_manager
    if args.test:
        import asyncio
        asyncio.run(alert_manager.info("Test Alert", "CLI triggered test message"))
        print("Test alert sent via console")
        return 0
    print("AlertManager ready")
    return 0


def cmd_signal(args: argparse.Namespace) -> int:
    """生成交易信号（直接调用 ai_engine）。"""
    try:
        from ai_engine.signal_scorer import SignalScorer
        scorer = SignalScorer()
        print(f"SignalScorer initialized")
        print("Use --symbol to generate signals for a specific pair")
        return 0
    except ImportError as exc:
        print(f"Error: {exc}")
        return 1


def cmd_test(args: argparse.Namespace) -> int:
    """运行测试。"""
    target = f"tests/test_{args.module}.py" if args.module else "tests/"
    cmd = [sys.executable, "-m", "pytest", target, "-v"]
    if args.tb:
        cmd.extend(["--tb", args.tb])
    if args.keyword:
        cmd.extend(["-k", args.keyword])
    if args.coverage:
        cmd = [sys.executable, "-m", "coverage", "run", "-m", "pytest", target]
        result = subprocess.run(cmd)
        subprocess.run([sys.executable, "-m", "coverage", "report"])
        return result.returncode
    result = subprocess.run(cmd)
    return result.returncode


def cmd_check_env(args: argparse.Namespace) -> int:
    """检查环境配置。"""
    checks = []
    modules = [
        ("risk_guardian", "risk_guardian/__init__.py"),
        ("indicators", "indicators/__init__.py"),
        ("analysis", "analysis/__init__.py"),
        ("validation", "validation/__init__.py"),
        ("observability", "observability/__init__.py"),
        ("ui", "ui/__init__.py"),
        ("scripts", "scripts/__init__.py"),
    ]
    for name, path in modules:
        ok = os.path.isfile(path)
        checks.append((name, ok))
        status = "OK" if ok else "MISSING"
        print(f"  [{status:7s}] {name} ({path})")

    env_vars = [
        "TIMESCALEDB_HOST", "TIMESCALEDB_PORT",
        "INFLUXDB_URL", "INFLUXDB_ORG",
        "FREQTRADE_API_URL",
        "MAX_DAILY_DRAWDOWN_PCT",
    ]
    for var in env_vars:
        val = os.getenv(var)
        status = "SET" if val else "NOT SET"
        print(f"  [{status:8s}] {var}" + (f" = {val}" if val else ""))

    missing = [n for n, ok in checks if not ok]
    if missing:
        print(f"\nWarning: {len(missing)} module(s) missing")
        return 1
    print(f"\nAll {len(modules)} modules present")
    return 0


def cmd_hmm_train(args: argparse.Namespace) -> int:
    """HMM 离线训练。"""
    from scripts.train_hmm import main as hmm_main
    sys.argv = ["train_hmm.py"]
    if args.all_major:
        sys.argv.append("--all-major")
    if args.symbol:
        sys.argv.extend(["--symbol", args.symbol])
    if args.timeframe:
        sys.argv.extend(["--timeframe", args.timeframe])
    if args.force_refresh:
        sys.argv.append("--force-refresh")
    if args.concurrency:
        sys.argv.extend(["--concurrency", str(args.concurrency)])
    if args.list_symbols:
        sys.argv.append("--list-symbols")
    if args.check_all:
        sys.argv.append("--check-all")
    if args.list_models:
        sys.argv.append("--list-models")
    if args.verbose:
        sys.argv.append("--verbose")
    return hmm_main()


def cmd_run(args: argparse.Namespace) -> int:
    """运行任意 Python 模块。"""
    module = args.module
    try:
        mod = importlib.import_module(module)
        if hasattr(mod, "main"):
            return mod.main()
        print(f"Module {module} has no main() function")
        return 1
    except ImportError as exc:
        print(f"Error importing {module}: {exc}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crypto AI Trader - Unified CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m scripts.cli_main health --service redis
  python -m scripts.cli_main dashboard
  python -m scripts.cli_main backfill --symbol BTCUSDT --days 7
  python -m scripts.cli_main decay --once
  python -m scripts.cli_main test --module health_check
  python -m scripts.cli_main check-env
        """,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # health
    p_health = subparsers.add_parser("health", help="Run health check")
    p_health.add_argument("--service", action="append", dest="services", default=[], help="Service to check")
    p_health.add_argument("--json", action="store_true", help="JSON output")

    # dashboard
    subparsers.add_parser("dashboard", help="Start web dashboard")

    # backfill
    p_bf = subparsers.add_parser("backfill", help="Data backfill")
    p_bf.add_argument("--symbol", type=str, default=None, help="Trading pair")
    p_bf.add_argument("--all-major", action="store_true", help="All major pairs")
    p_bf.add_argument("--list-symbols", action="store_true", help="List available symbols")
    p_bf.add_argument("--interval", type=str, default=None, help="Kline interval")
    p_bf.add_argument("--start", type=str, default=None, help="Start date")
    p_bf.add_argument("--end", type=str, default=None, help="End date")
    p_bf.add_argument("--days", type=int, default=None, help="Days to backfill")
    p_bf.add_argument("--exchange", type=str, default=None, help="Exchange name")
    p_bf.add_argument("--concurrency", type=int, default=None, help="Max concurrency")

    # decay
    p_decay = subparsers.add_parser("decay", help="Factor decay check")
    p_decay.add_argument("--once", action="store_true", help="Run once and exit")
    p_decay.add_argument("--factor", type=str, default=None, help="Factor name")
    p_decay.add_argument("--metrics", action="store_true", help="Prometheus metrics output")

    # alert
    p_alert = subparsers.add_parser("alert", help="Alert manager")
    p_alert.add_argument("--test", action="store_true", help="Send test alert")
    p_alert.add_argument("--config", type=str, default=None, help="Config file")

    # signal
    p_signal = subparsers.add_parser("signal", help="Generate trading signal")
    p_signal.add_argument("--symbol", type=str, required=True, help="Trading pair")

    # test
    p_test = subparsers.add_parser("test", help="Run tests")
    p_test.add_argument("--module", type=str, default=None, help="Test module name (without test_)")
    p_test.add_argument("--tb", type=str, default="line", help="Traceback mode")
    p_test.add_argument("--keyword", type=str, default=None, help="Filter by keyword")
    p_test.add_argument("--coverage", action="store_true", help="Run with coverage")

    # hmm-train
    p_hmm = subparsers.add_parser("hmm-train", help="Train HMM regime models")
    sym_group = p_hmm.add_mutually_exclusive_group()
    sym_group.add_argument("--symbol", type=str, default=None, help="Trading pair(s), comma-separated")
    sym_group.add_argument("--all-major", action="store_true", help="Train all 12 major pairs")
    p_hmm.add_argument("--timeframe", type=str, default=None, help="Timeframe(s), comma-separated")
    p_hmm.add_argument("--force-refresh", action="store_true", help="Ignore cache, fetch from Binance")
    p_hmm.add_argument("--concurrency", type=int, default=None, help="Max concurrent training tasks")
    p_hmm.add_argument("--list-symbols", action="store_true", help="List available symbols and exit")
    p_hmm.add_argument("--list-models", action="store_true", help="List saved model files")
    p_hmm.add_argument("--check-all", action="store_true", help="Check model status without training")
    p_hmm.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    # check-env
    subparsers.add_parser("check-env", help="Check environment")

    # run
    p_run = subparsers.add_parser("run", help="Run arbitrary Python module")
    p_run.add_argument("module", type=str, help="Module path (e.g. scripts.health_check)")

    args = parser.parse_args()

    cmd_map = {
        "health": cmd_health,
        "dashboard": cmd_dashboard,
        "backfill": cmd_backfill,
        "decay": cmd_decay,
        "alert": cmd_alert,
        "signal": cmd_signal,
        "test": cmd_test,
        "check-env": cmd_check_env,
        "hmm-train": cmd_hmm_train,
        "run": cmd_run,
    }

    if args.command is None:
        parser.print_help()
        return 1

    return cmd_map[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
