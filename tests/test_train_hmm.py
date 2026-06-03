"""
train_hmm.py 测试套件
覆盖：TrainResult / TrainSummary 模型、HMMCLITrainer 编排、
      format_summary / format_check_report 格式化、CLI 解析与主入口
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.train_hmm import (
    TrainResult,
    TrainSummary,
    HMMCLITrainer,
    format_summary,
    format_check_report,
    parse_args,
    main,
    _resolve_symbols,
    _resolve_timeframes,
    DEFAULT_SYMBOLS,
    DEFAULT_TIMEFRAMES,
)


# ─── 数据模型测试 ─────────────────────────────────────────

class TestTrainResult:
    def test_defaults(self):
        r = TrainResult(symbol="BTCUSDT", timeframe="1h", success=True)
        assert r.symbol == "BTCUSDT"
        assert r.timeframe == "1h"
        assert r.success is True
        assert r.elapsed_s == 0.0
        assert r.error == ""

    def test_failure(self):
        r = TrainResult(symbol="ETHUSDT", timeframe="4h", success=False, error="timeout")
        assert r.success is False
        assert r.error == "timeout"

    def test_regime_map(self):
        r = TrainResult(
            symbol="BTCUSDT", timeframe="1h", success=True,
            regime_map={0: "TRENDING", 1: "RANGING", 2: "HIGH_VOLATILITY"},
        )
        assert r.regime_map[0] == "TRENDING"
        assert r.regime_map[2] == "HIGH_VOLATILITY"


class TestTrainSummary:
    def test_defaults(self):
        s = TrainSummary()
        assert s.total == 0
        assert s.success == 0
        assert s.failed == 0

    def test_counts_correct(self):
        s = TrainSummary(
            total=5,
            results=[
                TrainResult("A", "1h", True),
                TrainResult("B", "1h", True),
                TrainResult("C", "1h", False),
            ],
        )
        # 手动设置计数（format_summary 依赖）
        s.success = 2
        s.failed = 1
        assert s.success == 2
        assert s.failed == 1

    def test_empty_results(self):
        s = TrainSummary(total=0)
        assert len(s.results) == 0


# ─── 解析器测试 ───────────────────────────────────────────

class TestParseArgs:
    def test_default_symbol(self):
        """默认 symbol 应为 BTCUSDT。"""
        args = parse_args([])
        assert _resolve_symbols(args) == ["BTCUSDT"]

    def test_single_symbol(self):
        args = parse_args(["--symbol", "BTCUSDT"])
        assert _resolve_symbols(args) == ["BTCUSDT"]

    def test_multi_symbol(self):
        args = parse_args(["--symbol", "BTCUSDT,ETHUSDT"])
        assert _resolve_symbols(args) == ["BTCUSDT", "ETHUSDT"]

    def test_all_major(self):
        args = parse_args(["--all-major"])
        assert _resolve_symbols(args) == DEFAULT_SYMBOLS

    def test_default_timeframe(self):
        args = parse_args([])
        assert _resolve_timeframes(args) == ["1h", "4h", "1d"]

    def test_custom_timeframe(self):
        args = parse_args(["--timeframe", "1h,4h"])
        assert _resolve_timeframes(args) == ["1h", "4h"]

    def test_single_timeframe(self):
        args = parse_args(["--timeframe", "1d"])
        assert _resolve_timeframes(args) == ["1d"]

    def test_invalid_timeframe_exits(self):
        """无效周期应导致 sys.exit(1)。"""
        with pytest.raises(SystemExit):
            _resolve_timeframes(parse_args(["--timeframe", "xyz"]))

    def test_list_symbols(self):
        args = parse_args(["--list-symbols"])
        assert args.list_symbols is True

    def test_check_all(self):
        args = parse_args(["--check-all"])
        assert args.check_all is True

    def test_force_refresh(self):
        args = parse_args(["--symbol", "BTCUSDT", "--force-refresh"])
        assert args.force_refresh is True

    def test_concurrency(self):
        args = parse_args(["--concurrency", "5"])
        assert args.concurrency == 5

    def test_verbose(self):
        args = parse_args(["--verbose"])
        assert args.verbose is True

    def test_list_models(self):
        args = parse_args(["--list-models"])
        assert args.list_models is True

    def test_symbol_and_all_major_mutually_exclusive(self):
        """--symbol 和 --all-major 应互斥。"""
        with pytest.raises(SystemExit):
            parse_args(["--symbol", "BTCUSDT", "--all-major"])


# ─── HMMCLITrainer 测试 ──────────────────────────────────

class TestHMMCLITrainer:
    def test_init_defaults(self):
        trainer = HMMCLITrainer()
        assert trainer._concurrency == 3
        assert trainer._force_refresh is False
        assert trainer._check_only is False

    def test_init_custom(self):
        trainer = HMMCLITrainer(concurrency=5, force_refresh=True, check_only=True)
        assert trainer._concurrency == 5
        assert trainer._force_refresh is True
        assert trainer._check_only is True

    @pytest.mark.asyncio
    async def test_train_one_success(self):
        """模拟训练成功流程。"""
        trainer = HMMCLITrainer(concurrency=1)

        mock_artifact = MagicMock()
        mock_artifact.model.n_components = 3
        mock_artifact.feature_names = ["a", "b", "c", "d", "e"]
        mock_artifact.state_regime_map = {0: "TRENDING", 1: "RANGING", 2: "HIGH_VOLATILITY"}
        mock_artifact.model.monitor_.history = [-100.5]
        mock_artifact.model.monitor_.converged = True
        mock_artifact.model.monitor_.iter = 150

        with patch("scripts.train_hmm.HMMTrainer") as MockTrainer:
            instance = MockTrainer.return_value
            instance.train = AsyncMock(return_value=mock_artifact)
            instance.save.return_value = Path("/tmp/test.pkl")

            result = await trainer.train_one("BTCUSDT", "1h")

        assert result.success is True
        assert result.symbol == "BTCUSDT"
        assert result.timeframe == "1h"
        assert result.converged is True
        assert result.iterations == 150
        assert result.regime_map[0] == "TRENDING"
        assert result.elapsed_s >= 0

    @pytest.mark.asyncio
    async def test_train_one_failure(self):
        """模拟训练失败。"""
        trainer = HMMCLITrainer(concurrency=1)

        with patch("scripts.train_hmm.HMMTrainer") as MockTrainer:
            instance = MockTrainer.return_value
            instance.train = AsyncMock(return_value=None)

            result = await trainer.train_one("BTCUSDT", "1h")

        assert result.success is False
        assert "失败" in result.error

    @pytest.mark.asyncio
    async def test_train_one_exception(self):
        """模拟训练异常。"""
        trainer = HMMCLITrainer(concurrency=1)

        with patch("scripts.train_hmm.HMMTrainer") as MockTrainer:
            instance = MockTrainer.return_value
            instance.train = AsyncMock(side_effect=RuntimeError("API error"))

            result = await trainer.train_one("BTCUSDT", "1h")

        assert result.success is False
        assert "API error" in result.error

    @pytest.mark.asyncio
    async def test_train_all_generates_summary(self):
        """train_all 应正确统计成功/失败。"""
        trainer = HMMCLITrainer(concurrency=2)

        # side_effect 长度必须匹配任务数：2 symbols × 2 timeframes = 4
        with patch.object(trainer, "train_one") as mock_one:
            mock_one.side_effect = [
                TrainResult("BTCUSDT", "1h", True, elapsed_s=1.0),
                TrainResult("BTCUSDT", "4h", True, elapsed_s=2.0),
                TrainResult("ETHUSDT", "1h", False, elapsed_s=0.5, error="no data"),
                TrainResult("ETHUSDT", "4h", True, elapsed_s=0.8),
            ]

            summary = await trainer.train_all(["BTCUSDT", "ETHUSDT"], ["1h", "4h"])

        assert summary.total == 4
        assert summary.success == 3
        assert summary.failed == 1

    @pytest.mark.asyncio
    async def test_train_all_single_pair(self):
        """单交易对单周期的训练汇总。"""
        trainer = HMMCLITrainer(concurrency=1)

        with patch.object(trainer, "train_one") as mock_one:
            mock_one.return_value = TrainResult("BTCUSDT", "1h", True, elapsed_s=1.5)

            summary = await trainer.train_all(["BTCUSDT"], ["1h"])

        assert summary.total == 1
        assert summary.success == 1
        assert summary.failed == 0
        assert summary.skipped == 0


# ─── 格式化测试 ───────────────────────────────────────────

class TestFormatSummary:
    def test_empty_summary(self):
        s = TrainSummary(total=0)
        output = format_summary(s)
        assert "训练报告" in output
        assert "0" in output

    def test_all_success(self):
        s = TrainSummary(
            total=2,
            success=2,
            failed=0,
            results=[
                TrainResult("BTCUSDT", "1h", True, elapsed_s=1.2, converged=True, iterations=100, log_likelihood=-50.0, regime_map={0: "TRENDING"}),
                TrainResult("BTCUSDT", "4h", True, elapsed_s=2.1, converged=True, iterations=150, log_likelihood=-60.0, regime_map={0: "RANGING"}),
            ],
        )
        output = format_summary(s)
        assert "成功" in output
        assert "2" in output
        assert "失败" in output
        assert "0" in output

    def test_verbose_includes_details(self):
        s = TrainSummary(
            total=1,
            success=1,
            results=[
                TrainResult("BTCUSDT", "1h", True, elapsed_s=1.0, converged=True, iterations=100, log_likelihood=-50.0, regime_map={0: "TRENDING", 1: "RANGING"}),
            ],
        )
        output = format_summary(s, verbose=True)
        assert "S0→TRENDING" in output
        assert "S1→RANGING" in output
        assert "100 iter" in output

    def test_has_failures(self):
        s = TrainSummary(
            total=2,
            success=1,
            failed=1,
            results=[
                TrainResult("BTCUSDT", "1h", True),
                TrainResult("ETHUSDT", "1h", False, error="no data"),
            ],
        )
        output = format_summary(s)
        assert "失败详情" in output
        assert "ETHUSDT" in output
        assert "no data" in output


class TestFormatCheckReport:
    def test_all_existing(self):
        results = [
            TrainResult("BTCUSDT", "1h", True, error="模型存在（2天前训练） ✅有效"),
            TrainResult("ETHUSDT", "1h", True, error="模型存在（2天前训练） ✅有效"),
        ]
        output = format_check_report(results)
        assert "有效模型: 2" in output
        assert "缺失模型: 0" in output
        assert "BTCUSDT" in output

    def test_mixed(self):
        results = [
            TrainResult("BTCUSDT", "1h", True, error="模型存在（5天前训练） ⚠️需要重训"),
            TrainResult("ETHUSDT", "1h", False, error="模型不存在"),
        ]
        output = format_check_report(results)
        assert "需要重训" in output
        assert "缺失模型: 1" in output
        assert "ETHUSDT" in output


# ─── CLI 主入口测试 ───────────────────────────────────────

class TestMain:
    def test_list_symbols(self):
        """--list-symbols 应返回 0。"""
        with patch("sys.argv", ["train_hmm.py", "--list-symbols"]):
            rc = main()
            assert rc == 0

    def test_list_models(self):
        """--list-models 应返回 0。"""
        with patch("sys.argv", ["train_hmm.py", "--list-models"]):
            rc = main()
            assert rc == 0

    def test_verbose_training(self):
        """训练流程应正常执行（mock HMMTrainer）。"""
        mock_artifact = MagicMock()
        mock_artifact.model.n_components = 3
        mock_artifact.feature_names = ["a", "b", "c", "d", "e"]
        mock_artifact.state_regime_map = {0: "TRENDING", 1: "RANGING", 2: "HIGH_VOLATILITY"}
        mock_artifact.model.monitor_.history = [-100.0]
        mock_artifact.model.monitor_.converged = True
        mock_artifact.model.monitor_.iter = 120

        with patch("scripts.train_hmm.HMMTrainer") as MockTrainer:
            instance = MockTrainer.return_value
            instance.train = AsyncMock(return_value=mock_artifact)
            instance.save.return_value = Path("/tmp/test.pkl")
            instance.needs_retrain.return_value = True

            with patch("sys.argv", ["train_hmm.py", "--symbol", "BTCUSDT", "--timeframe", "1h"]):
                rc = main()
                assert rc == 0

    def test_training_failure(self):
        """训练失败应返回 1。"""
        with patch("scripts.train_hmm.HMMTrainer") as MockTrainer:
            instance = MockTrainer.return_value
            instance.train = AsyncMock(return_value=None)
            instance.needs_retrain.return_value = True

            with patch("sys.argv", ["train_hmm.py", "--symbol", "BTCUSDT", "--timeframe", "1h"]):
                rc = main()
                assert rc == 1

    def test_training_exception(self):
        """训练异常应返回 1。"""
        with patch("scripts.train_hmm.HMMTrainer") as MockTrainer:
            instance = MockTrainer.return_value
            instance.train = AsyncMock(side_effect=RuntimeError("API failed"))
            instance.needs_retrain.return_value = True

            with patch("sys.argv", ["train_hmm.py", "--symbol", "BTCUSDT", "--timeframe", "1h"]):
                rc = main()
                assert rc == 1

    def test_check_all_with_no_models(self):
        """--check-all 时所有模型不存在。"""
        with patch("scripts.train_hmm.HMMTrainer") as MockTrainer:
            instance = MockTrainer.return_value
            instance.needs_retrain.return_value = True
            instance.load.return_value = None

            with patch("sys.argv", ["train_hmm.py", "--check-all", "--symbol", "BTCUSDT", "--timeframe", "1h"]):
                rc = main()
                assert rc == 0  # 检查模式始终返回 0


# ─── 模块级测试 ───────────────────────────────────────────

class TestModuleLevel:
    def test_default_symbols_list(self):
        assert len(DEFAULT_SYMBOLS) == 12
        assert "BTCUSDT" in DEFAULT_SYMBOLS
        assert "ETHUSDT" in DEFAULT_SYMBOLS

    def test_default_timeframes(self):
        assert DEFAULT_TIMEFRAMES == ["1h", "4h", "1d"]

    def test_importable(self):
        from scripts import train_hmm
        assert hasattr(train_hmm, "main")
        assert hasattr(train_hmm, "HMMCLITrainer")
        assert hasattr(train_hmm, "TrainResult")
        assert hasattr(train_hmm, "TrainSummary")
