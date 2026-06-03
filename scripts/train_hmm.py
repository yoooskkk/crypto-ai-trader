"""
HMM 离线训练脚本 — 拉取历史 K 线 → 训练 GaussianHMM → 保存模型

用法:
    # 训练单个交易对
    python -m scripts.train_hmm --symbol BTCUSDT --timeframe 1h

    # 训练多个交易对 + 多个周期
    python -m scripts.train_hmm --symbol BTCUSDT,ETHUSDT --timeframe 1h,4h

    # 主流币全训（12 个币 + 3 个周期 = 36 个模型）
    python -m scripts.train_hmm --all-major --timeframe 1h,4h,1d

    # 强制从 Binance 拉取（跳过缓存）
    python -m scripts.train_hmm --symbol BTCUSDT --force-refresh

    # 并发训练
    python -m scripts.train_hmm --all-major --concurrency 5

    # 列出可用交易对 + 检查现有模型
    python -m scripts.train_hmm --list-symbols
    python -m scripts.train_hmm --check-all

返回值: 0（全部成功） / 1（存在失败）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from regime.hmm_model import HMMTrainer, HMMModelArtifact, _MODELS_DIR, RETRAIN_DAYS

logger = structlog.get_logger(__name__)

# ─── 主流币列表（与 backfill_data.py 保持一致） ─────────────

DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT",
    "DOGEUSDT", "XRPUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "DOTUSDT", "MATICUSDT", "ATOMUSDT",
]

DEFAULT_TIMEFRAMES = ["1h", "4h", "1d"]

# ─── 训练结果模型 ─────────────────────────────────────────

@dataclass
class TrainResult:
    """单个训练任务的结果。"""
    symbol: str
    timeframe: str
    success: bool
    elapsed_s: float = 0.0
    n_samples: int = 0
    n_features: int = 0
    regime_map: dict[int, str] = field(default_factory=dict)
    log_likelihood: float | None = None
    converged: bool = False
    iterations: int = 0
    error: str = ""
    from_cache: bool = False
    model_path: str = ""


@dataclass
class TrainSummary:
    """全局训练汇总。"""
    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    elapsed_s: float = 0.0
    results: list[TrainResult] = field(default_factory=list)


# ─── 训练器 ───────────────────────────────────────────────

class HMMCLITrainer:
    """
    CLI 专用训练编排器。封装 HMMTrainer，添加并发、报告、缓存检查。
    """

    def __init__(
        self,
        concurrency: int = 3,
        force_refresh: bool = False,
        check_only: bool = False,
    ):
        self._concurrency = concurrency
        self._force_refresh = force_refresh
        self._check_only = check_only
        self._semaphore = asyncio.Semaphore(concurrency)

    async def train_one(
        self,
        symbol: str,
        timeframe: str,
    ) -> TrainResult:
        """训练单个模型。"""
        start = time.monotonic()
        trainer = HMMTrainer()

        # 检查缓存
        cached_data = False
        if not self._force_refresh and not self._check_only:
            if not trainer.needs_retrain(symbol, timeframe):
                logger.info("模型已是最新，跳过", symbol=symbol, timeframe=timeframe)
                return TrainResult(
                    symbol=symbol,
                    timeframe=timeframe,
                    success=True,
                    elapsed_s=0.0,
                    from_cache=True,
                    model_path=str(_MODELS_DIR / f"{symbol}_{timeframe}.pkl"),
                    error="模型已是最新，跳过训练",
                )

        if self._check_only:
            exists = _MODELS_DIR.joinpath(f"{symbol}_{timeframe}.pkl").exists()
            needs = trainer.needs_retrain(symbol, timeframe)
            if exists:
                artifact = trainer.load(symbol, timeframe)
                age_days = 0
                if artifact:
                    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
                        artifact.train_timestamp / 1000, tz=timezone.utc
                    )
                    age_days = age.days
                return TrainResult(
                    symbol=symbol,
                    timeframe=timeframe,
                    success=True,
                    from_cache=True,
                    model_path=str(_MODELS_DIR / f"{symbol}_{timeframe}.pkl"),
                    n_samples=0,
                    error=f"模型存在（{age_days}天前训练）{' ⚠️需要重训' if needs else ' ✅有效'}",
                )
            return TrainResult(
                symbol=symbol,
                timeframe=timeframe,
                success=False,
                error="模型不存在",
            )

        async with self._semaphore:
            try:
                artifact = await trainer.train(symbol, timeframe, force_refresh=self._force_refresh)
            except Exception as exc:
                elapsed = time.monotonic() - start
                logger.error("训练异常", symbol=symbol, timeframe=timeframe, error=str(exc))
                return TrainResult(
                    symbol=symbol,
                    timeframe=timeframe,
                    success=False,
                    elapsed_s=round(elapsed, 2),
                    error=str(exc),
                )

        elapsed = time.monotonic() - start

        if artifact is None:
            return TrainResult(
                symbol=symbol,
                timeframe=timeframe,
                success=False,
                elapsed_s=round(elapsed, 2),
                error="训练失败（数据不足或特征提取异常）",
            )

        # 保存模型
        model_path = trainer.save(artifact)

        return TrainResult(
            symbol=symbol,
            timeframe=timeframe,
            success=True,
            elapsed_s=round(elapsed, 2),
            n_samples=artifact.model.n_components,  # 隐状态数
            n_features=len(artifact.feature_names),
            regime_map={k: v for k, v in artifact.state_regime_map.items()},
            log_likelihood=(
                artifact.model.monitor_.history[-1]
                if artifact.model.monitor_.history else None
            ),
            converged=artifact.model.monitor_.converged,
            iterations=artifact.model.monitor_.iter,
            from_cache=False,
            model_path=str(model_path),
        )

    async def train_all(
        self,
        symbols: list[str],
        timeframes: list[str],
    ) -> TrainSummary:
        """训练所有组合。"""
        tasks: list[asyncio.Task[TrainResult]] = []
        total = len(symbols) * len(timeframes)
        logger.info(
            "HMM 批量训练启动",
            symbols=len(symbols),
            timeframes=len(timeframes),
            total=total,
            concurrency=self._concurrency,
            force_refresh=self._force_refresh,
        )

        start_total = time.monotonic()

        for symbol in symbols:
            for tf in timeframes:
                tasks.append(asyncio.create_task(self.train_one(symbol, tf)))

        results = await asyncio.gather(*tasks)
        elapsed_total = time.monotonic() - start_total

        summary = TrainSummary(
            total=total,
            elapsed_s=round(elapsed_total, 2),
            results=list(results),
        )

        for r in results:
            if r.from_cache:
                summary.skipped += 1
            elif r.success:
                summary.success += 1
            else:
                summary.failed += 1

        return summary


# ─── 报告格式化 ───────────────────────────────────────────

def format_summary(summary: TrainSummary, verbose: bool = False) -> str:
    """格式化为人类可读的报告。"""
    lines = [
        "═══════════════════════════════════════════",
        "  HMM 模型训练报告",
        f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "═══════════════════════════════════════════",
        f"  总计:      {summary.total}",
        f"  ✅ 成功:   {summary.success}",
        f"  ⏭️ 跳过:   {summary.skipped}",
        f"  ❌ 失败:   {summary.failed}",
        f"  耗时:      {summary.elapsed_s}s",
    ]

    if summary.failed > 0:
        lines.append("  ── 失败详情 ──")
        for r in summary.results:
            if not r.success and not r.from_cache:
                lines.append(f"    ❌ {r.symbol} ({r.timeframe}): {r.error}")

    if verbose and summary.success > 0:
        lines.append("  ── 成功详情 ──")
        for r in summary.results:
            if r.success:
                conv = "✅" if r.converged else "⚠️"
                maps = ", ".join(f"S{k}→{v}" for k, v in sorted(r.regime_map.items()))
                lines.append(
                    f"    {conv} {r.symbol} ({r.timeframe}) "
                    f"[{r.iterations} iter, LL={r.log_likelihood:.1f}] "
                    f"映射: {maps}"
                )

    if summary.skipped > 0:
        lines.append(f"  ⏭️ {summary.skipped} 个模型在 {RETRAIN_DAYS} 天内已训练，已跳过")

    return "\n".join(lines)


def format_check_report(results: list[TrainResult]) -> str:
    """格式化为模型检查报告。"""
    lines = [
        "═══════════════════════════════════════════",
        "  HMM 模型状态检查",
        f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "═══════════════════════════════════════════",
    ]

    existing = [r for r in results if r.success and "存在" in r.error]
    missing = [r for r in results if not r.success and "不存在" in r.error]
    needs_retrain = [r for r in results if r.success and "需要重训" in r.error]

    lines.append(f"  ✅ 有效模型: {len(existing)}")
    lines.append(f"  ⚠️ 需要重训: {len(needs_retrain)}")
    lines.append(f"  ❌ 缺失模型: {len(missing)}")

    if existing:
        lines.append("  ── 有效模型 ──")
        for r in existing:
            lines.append(f"    ✅ {r.symbol} ({r.timeframe}) — {r.error}")

    if needs_retrain:
        lines.append("  ── 需要重训 ──")
        for r in needs_retrain:
            lines.append(f"    ⚠️ {r.symbol} ({r.timeframe}) — {r.error}")

    if missing:
        lines.append("  ── 缺失模型 ──")
        for r in missing:
            lines.append(f"    ❌ {r.symbol} ({r.timeframe}) — {r.error}")

    return "\n".join(lines)


# ─── 主逻辑 ───────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HMM 离线训练脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # 交易对选择
    symbol_group = parser.add_mutually_exclusive_group()
    symbol_group.add_argument(
        "--symbol", type=str, default=None,
        help="交易对（逗号分隔，如 BTCUSDT,ETHUSDT）",
    )
    symbol_group.add_argument(
        "--all-major", action="store_true",
        help="训练所有主流币（12 个）",
    )

    # 时间周期
    parser.add_argument(
        "--timeframe", type=str, default="1h,4h,1d",
        help="时间周期（逗号分隔，默认 1h,4h,1d）",
    )

    # 训练控制
    parser.add_argument(
        "--force-refresh", action="store_true",
        help="强制从 Binance 拉取数据（忽略本地缓存）",
    )
    parser.add_argument(
        "--concurrency", type=int, default=3,
        help="最大并发训练数（默认 3）",
    )

    # 信息命令
    parser.add_argument(
        "--list-symbols", action="store_true",
        help="列出可用的交易对并退出",
    )
    parser.add_argument(
        "--check-all", action="store_true",
        help="检查所有模型状态（不训练）",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="详细输出训练结果",
    )
    parser.add_argument(
        "--list-models", action="store_true",
        help="列出已保存的模型文件",
    )

    return parser.parse_args(argv)


def _resolve_symbols(args: argparse.Namespace) -> list[str]:
    """解析交易对列表。"""
    if args.all_major:
        return list(DEFAULT_SYMBOLS)
    if args.symbol:
        return [s.strip().upper() for s in args.symbol.split(",") if s.strip()]
    return ["BTCUSDT"]


def _resolve_timeframes(args: argparse.Namespace) -> list[str]:
    """解析时间周期列表。"""
    tfs = [tf.strip() for tf in args.timeframe.split(",") if tf.strip()]
    valid = {"1m", "5m", "15m", "1h", "4h", "1d", "1w"}
    for tf in tfs:
        if tf not in valid:
            print(f"错误: 无效的时间周期 '{tf}'，可选: {', '.join(sorted(valid))}")
            sys.exit(1)
    return tfs


def cmd_list_symbols() -> int:
    """列出可用交易对和已保存的模型。"""
    print("可用的主流交易对:")
    for s in DEFAULT_SYMBOLS:
        print(f"  {s}")

    model_dir = _MODELS_DIR
    if model_dir.exists():
        models = list(model_dir.glob("*.pkl"))
        if models:
            print(f"\n已保存的模型 ({len(models)} 个):")
            for m in sorted(models):
                try:
                    from regime.hmm_model import HMMTrainer
                    trainer = HMMTrainer()
                    parts = m.stem.split("_")
                    if len(parts) >= 2:
                        symbol = parts[0].upper()
                        tf = parts[-1] if len(parts) > 2 else parts[1]
                        needs = trainer.needs_retrain(symbol, tf)
                        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
                            trainer.load(symbol, tf).train_timestamp / 1000
                        ) if trainer.load(symbol, tf) else None
                        age_str = f"{age.days}d" if age else "?"
                        print(f"    {'⚠️' if needs else '✅'} {m.name} ({age_str})")
                    else:
                        print(f"    {'  '} {m.name}")
                except Exception:
                    print(f"    {'  '} {m.name}")
        else:
            print("\n暂无已保存的模型。使用 --symbol BTCUSDT --timeframe 1h 训练。")

    return 0


def cmd_list_models() -> int:
    """列出已保存的模型文件。"""
    model_dir = _MODELS_DIR
    if not model_dir.exists():
        print("models/hmm/ 目录不存在")
        return 0

    models = list(model_dir.glob("*.pkl"))
    if not models:
        print("暂无已保存的模型文件")
        return 0

    print(f"已保存的 HMM 模型 ({len(models)} 个):")
    for m in sorted(models):
        size_kb = m.stat().st_size / 1024
        mtime = datetime.fromtimestamp(m.stat().st_mtime, tz=timezone.utc)
        print(f"  {m.name} ({size_kb:.1f} KB, {mtime.strftime('%Y-%m-%d %H:%M')})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 信息命令
    if args.list_symbols:
        return cmd_list_symbols()
    if args.list_models:
        return cmd_list_models()

    symbols = _resolve_symbols(args)
    timeframes = _resolve_timeframes(args)

    print(f"交易对: {', '.join(symbols)}")
    print(f"周期:   {', '.join(timeframes)}")
    print(f"并发:   {args.concurrency}")
    if args.force_refresh:
        print("模式:   强制刷新（忽略缓存）")
    if args.check_all:
        print("模式:   仅检查（不训练）")

    total = len(symbols) * len(timeframes)
    print(f"\n共 {total} 个训练任务\n")

    # 执行训练/检查
    async def run() -> TrainSummary | None:
        trainer = HMMCLITrainer(
            concurrency=args.concurrency,
            force_refresh=args.force_refresh,
            check_only=args.check_all,
        )

        if args.check_all:
            results: list[TrainResult] = []
            for symbol in symbols:
                for tf in timeframes:
                    result = await trainer.train_one(symbol, tf)
                    results.append(result)
            # 构建伪 summary 用于检查模式
            total_ = len(results)
            existing = sum(1 for r in results if r.success and "存在" in r.error)
            missing = sum(1 for r in results if not r.success and "不存在" in r.error)
            print(format_check_report(results))
            return None  # 检查模式不返回 summary
        else:
            summary = await trainer.train_all(symbols, timeframes)
            return summary

    try:
        result = asyncio.run(run())
    except KeyboardInterrupt:
        print("\n\n训练被用户中断")
        return 1
    except Exception as exc:
        print(f"\n训练异常: {exc}")
        logger.error("训练执行异常", error=str(exc))
        return 1

    if result is None:
        # 检查模式
        return 0

    # 输出报告
    print(format_summary(result, verbose=args.verbose))

    if result.failed > 0:
        print("\n⚠️ 部分模型训练失败，请检查日志")
        return 1

    if result.success == 0 == result.failed:
        print("\n全部模型已是最新，无需训练")
        return 0

    print(f"\n✅ 训练完成: {result.success} 成功, {result.skipped} 跳过, {result.failed} 失败")
    return 0


if __name__ == "__main__":
    sys.exit(main())
