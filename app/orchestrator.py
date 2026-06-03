"""
模块名称: orchestrator.py
所属层级: 基础设施层 (Infrastructure) — 系统编配
输入来源: 无（启动所有 worker）
输出去向: 无（管理各 worker 生命周期）
关键依赖: logging_setup, messaging.consumer (run_consumer), data.ws_client

crypto-ai-trader 主编排器。

用法 (Docker):
    python -m app.orchestrator --worker data          # 采集层
    python -m app.orchestrator --worker indicators    # 指标计算层
    python -m app.orchestrator --worker regime        # 制度识别层
    python -m app.orchestrator --worker ai_engine     # AI 引擎层
    python -m app.orchestrator --worker risk          # 风控层

用法 (单进程开发模式):
    python -m app.orchestrator                       # 启动所有 worker

铁律:
    - 所有服务共享 Redis Stream，禁止 HTTP 同步调用（ARCH.md #7）
    - 密钥从 .env / secrets/ 加载，不出现在日志中（ARCH.md #4）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from typing import NoReturn

import structlog

# ─── 必须在所有 import 完成前初始化日志 ───────────────────────
from logging_setup import setup_logging

# 默认调用 setup_logging()；JSON 格式由环境变量 LOG_JSON 控制
setup_logging(
    level=os.getenv("LOG_LEVEL", "INFO"),
    json_format=os.getenv("LOG_JSON", "").lower() in ("1", "true", "yes"),
)

logger = structlog.get_logger(__name__)


# ─── Worker 注册表 ────────────────────────────────────────────

async def _run_data_worker() -> NoReturn:
    """数据采集 worker：启动 Binance WebSocket 客户端。"""
    from data.ws_client import BinanceWSClient

    symbols_str = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT")
    symbols = [s.strip().upper() for s in symbols_str.split(",") if s.strip()]
    interval = os.getenv("KLINE_INTERVAL", "1m")

    logger.info("启动数据采集 worker", symbols=symbols, interval=interval)
    client = BinanceWSClient(symbols=symbols, interval=interval)
    await client.run()


async def _run_indicator_worker() -> NoReturn:
    """指标计算 worker：消费 raw_kline → 计算指标 → 写入 indicators Stream。"""
    from messaging.consumer import run_consumer
    from indicators.processor import process_raw_kline

    logger.info("启动指标计算 worker")
    await run_consumer(
        stream="raw_kline",
        group="indicators",
        consumer=f"indicator-{os.getpid()}",
        processor=process_raw_kline,
    )


async def _run_regime_worker() -> NoReturn:
    """制度识别 worker：消费 indicators → 制度检测 → 写入 regime_signal Stream。"""
    from messaging.consumer import run_consumer
    from regime.processor import process_indicators

    logger.info("启动制度识别 worker")
    await run_consumer(
        stream="indicators",
        group="regime",
        consumer=f"regime-{os.getpid()}",
        processor=process_indicators,
    )


async def _run_ai_engine_worker() -> NoReturn:
    """AI 引擎 worker：消费 regime_signal → PlanGenerator 生成计划 → 写入 ai_signal Stream。"""
    from messaging.consumer import run_consumer
    from ai_engine.processor import process_regime_signal

    logger.info("启动 AI 引擎 worker")
    await run_consumer(
        stream="regime_signal",
        group="ai_engine",
        consumer=f"ai-{os.getpid()}",
        processor=process_regime_signal,
    )


async def _run_risk_worker() -> NoReturn:
    """风控 worker：消费 ai_signal → 风险审核 → 写入 trade_order Stream。"""
    from messaging.consumer import run_consumer
    from risk_guardian.processor import process_ai_signal

    logger.info("启动风控 worker")
    await run_consumer(
        stream="ai_signal",
        group="risk_guardian",
        consumer=f"risk-{os.getpid()}",
        processor=process_ai_signal,
    )


# ─── Worker 映射 ──────────────────────────────────────────────

_WORKERS: dict[str, tuple[str, str]] = {
    "data":       ("数据采集",       "_run_data_worker"),
    "indicators": ("指标计算",       "_run_indicator_worker"),
    "regime":     ("制度识别",       "_run_regime_worker"),
    "ai_engine":  ("AI 引擎",        "_run_ai_engine_worker"),
    "risk":       ("风险控制",       "_run_risk_worker"),
}

_WORKER_FUNCS: dict[str, str] = {k: v[1] for k, v in _WORKERS.items()}


def _get_worker_func(name: str):
    """按名称获取 worker 协程函数。"""
    func_name = _WORKER_FUNCS.get(name)
    if func_name is None:
        raise ValueError(f"未知 worker: {name}，可选: {list(_WORKERS.keys())}")
    return globals()[func_name]


# ─── 信号处理 ─────────────────────────────────────────────────

_shutdown_event = asyncio.Event()


def _handle_signal(sig: int, frame) -> None:
    """收到退出信号时设置 shutdown 事件。"""
    signal_name = signal.Signals(sig).name
    logger.info("收到退出信号", signal=signal_name)
    _shutdown_event.set()


# ─── 单 worker 模式 ──────────────────────────────────────────

async def run_single_worker(worker_name: str) -> NoReturn:
    """运行单个 worker（Docker 容器模式）。"""
    coro = _get_worker_func(worker_name)
    try:
        await coro()
    except Exception:
        logger.exception("Worker 异常退出", worker=worker_name)
        sys.exit(1)


# ─── 多 worker 模式（开发用）─────────────────────────────────

async def run_all_workers() -> NoReturn:
    """启动所有 worker（单进程开发模式）。"""
    tasks: list[asyncio.Task] = []
    for name in _WORKERS:
        coro = _get_worker_func(name)
        task = asyncio.create_task(coro(), name=name)
        tasks.append(task)
        name_zh = _WORKERS[name][0]
        logger.info("已提交 worker 任务", worker=name, description=name_zh)

    # 等待任意一个 worker 退出（通常是异常）
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

    # 取消所有剩余任务
    for task in pending:
        task.cancel()

    # 检查是否有异常
    for task in done:
        exc = task.exception()
        if exc:
            logger.error("Worker 异常退出", worker=task.get_name(), error=str(exc))
            sys.exit(1)

    sys.exit(0)


# ─── 入口 ─────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="crypto-ai-trader 主编排器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python -m app.orchestrator --worker data\n"
            "  python -m app.orchestrator  # 开发模式，启动所有 worker\n"
        ),
    )
    parser.add_argument(
        "--worker",
        type=str,
        choices=list(_WORKERS.keys()),
        help="指定 worker 类型（缺省 = 开发模式，启动所有）",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    """主编排器入口。"""
    args = parse_args(argv)

    # 注册信号处理
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig, None)

    logger.info(
        "crypto-ai-trader 编排器启动",
        worker=args.worker or "ALL（开发模式）",
        log_json=os.getenv("LOG_JSON", "false"),
        pid=os.getpid(),
    )

    if args.worker:
        await run_single_worker(args.worker)
    else:
        await run_all_workers()


def entry_point() -> None:
    """控制台脚本入口（pyproject.toml 可注册）。"""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
