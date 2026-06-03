"""
模块名称: consumer.py
所属层级: 消息队列层 (Messaging)
输入来源: Redis Stream（通过 StreamConsumer 订阅）
输出去向: 由 processor 函数决定（写入另一个 Stream）
关键依赖: messaging.redis_stream (StreamConsumer), messaging.backpressure

通用 Redis Stream 消费者。

提供:
  1. StreamConsumer — 原 thin wrapper（兼容已有 import）
  2. run_consumer() — 异步消费者主循环（含背压检查、优雅退出）
  3. __main__ — CLI 入口（可由 docker-compose 直接调用）

用法:
    # 作为库
    from messaging.consumer import run_consumer
    await run_consumer(stream="raw_kline", group="mygroup", processor=my_func)

    # 作为 CLI（由 docker-compose.yml 调用）
    python -m messaging.consumer --stream raw_kline --group indicators
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from typing import Any, Awaitable, Callable

import structlog

from messaging.backpressure import check_backpressure, MAX_PENDING
from messaging.redis_stream import StreamConsumer as _StreamConsumer

# ─── 重新导出（兼容已有 import） ───────────────────────────────
StreamConsumer = _StreamConsumer

logger = structlog.get_logger(__name__)

# ─── Stream → Processor 映射 ─────────────────────────────────
# 当 CLI 只传 --group 时，根据 Stream 名称自动加载处理模块
_STREAM_PROCESSOR_MAP: dict[str, str] = {
    "raw_kline":     "indicators.processor:process_raw_kline",
    "indicators":    "regime.processor:process_indicators",
    "regime_signal": "ai_engine.processor:process_regime_signal",
    "ai_signal":     "risk_guardian.processor:process_ai_signal",
}

# ─── Stream → Output Stream 映射 ─────────────────────────────
_OUTPUT_STREAM_MAP: dict[str, str] = {
    "raw_kline":     "indicators",
    "indicators":    "regime_signal",
    "regime_signal": "ai_signal",
    "ai_signal":     "trade_order",
    "trade_order":   "trade_order",  # 终端 Stream
}

# ─── 信号处理 ─────────────────────────────────────────────────

_shutdown_event = asyncio.Event()


def _handle_signal(sig: int, frame) -> None:
    """收到退出信号时设置 shutdown 事件。"""
    signal_name = signal.Signals(sig).name
    logger.info("消费者收到退出信号", signal=signal_name)
    _shutdown_event.set()


# ─── 动态模块加载 ─────────────────────────────────────────────


def _load_processor(
    module_path: str,
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]:
    """
    动态加载处理器函数。

    参数:
        module_path: "module.submodule:function_name" 格式

    返回:
        处理器 async 函数
    """
    import importlib

    module_name, func_name = module_path.split(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        logger.error("处理器模块加载失败", module=module_name, error=str(exc))
        raise

    processor = getattr(module, func_name, None)
    if processor is None:
        raise AttributeError(f"模块 {module_name} 中未找到函数 {func_name}")

    if not asyncio.iscoroutinefunction(processor):
        raise TypeError(f"处理器 {module_path} 必须为 async 函数")

    logger.info("处理器加载成功", module=module_name, function=func_name)
    return processor


def _get_processor_for_stream(stream: str) -> Callable | None:
    """根据 Stream 名称获取默认处理器。"""
    module_path = _STREAM_PROCESSOR_MAP.get(stream)
    if module_path is None:
        return None
    try:
        return _load_processor(module_path)
    except (ImportError, AttributeError, TypeError) as exc:
        logger.error("默认处理器加载失败", stream=stream, error=str(exc))
        return None


# ─── 主消费循环 ───────────────────────────────────────────────


async def run_consumer(
    stream: str,
    group: str,
    consumer: str | None = None,
    processor: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None,
) -> None:
    """
    运行消费者主循环。

    1. 创建 StreamConsumer 并订阅
    2. 循环消费消息
    3. 调用 processor 处理每条消息
    4. 处理结果发布到对应的 output Stream（或 skip）
    5. 定期检查背压

    参数:
        stream: 要消费的 Redis Stream 名称
        group: 消费者组名
        consumer: 消费者名（默认 auto-{pid}-{seq}）
        processor: 处理函数（默认根据 stream 名称自动加载）
    """
    # 注册信号处理
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, _handle_signal, sig, None)
        except (NotImplementedError, ValueError):
            # Windows 不支持 add_signal_handler
            pass

    # 确定消费者名
    if consumer is None:
        consumer = f"auto-{os.getpid()}-{id(asyncio.get_event_loop())}"

    # 加载处理器
    if processor is None:
        processor = _get_processor_for_stream(stream)
        if processor is None:
            logger.error(
                "未指定处理器且无默认映射",
                stream=stream,
                available=list(_STREAM_PROCESSOR_MAP.keys()),
            )
            raise ValueError(
                f"Stream '{stream}' 没有默认处理器，请通过 --processor 指定 "
                f"或使用已映射的 Stream: {list(_STREAM_PROCESSOR_MAP.keys())}"
            )

    # ─── 输出 Stream ────────────────────────────────────
    output_stream: str = _OUTPUT_STREAM_MAP.get(stream, f"{stream}_processed")

    # ─── 创建消费者 + 生产者 ─────────────────────────────
    from messaging.producer import StreamProducer

    sc = _StreamConsumer(group=group, consumer=consumer)
    producer = StreamProducer()

    logger.info(
        "消费者启动",
        stream=stream,
        group=group,
        consumer=consumer,
        output_stream=output_stream,
        processor=processor.__name__,
    )

    # ─── 主循环 ───────────────────────────────────────────
    message_count = 0
    error_count = 0

    while not _shutdown_event.is_set():
        try:
            async for message in sc.subscribe(stream):
                if _shutdown_event.is_set():
                    break

                message_count += 1

                # 处理消息
                try:
                    result = await processor(message)
                except Exception as exc:
                    error_count += 1
                    logger.error(
                        "消息处理异常",
                        stream=stream,
                        error=str(exc),
                        message_id=message.get("ts", "N/A"),
                        symbol=message.get("symbol", "N/A"),
                    )
                    continue

                # 发布处理结果
                if result is not None:
                    try:
                        await producer.publish(output_stream, result)
                    except Exception as exc:
                        logger.error(
                            "结果发布失败",
                            stream=output_stream,
                            error=str(exc),
                        )

                # 定期日志
                if message_count % 100 == 0:
                    logger.info(
                        "消费者处理进度",
                        stream=stream,
                        messages=message_count,
                        errors=error_count,
                    )

                # 背压检查
                if message_count % 10 == 0:
                    try:
                        await check_backpressure(sc._r, stream)
                    except Exception:
                        pass

        except asyncio.CancelledError:
            logger.info("消费者被取消", stream=stream)
            break
        except Exception as exc:
            error_count += 1
            logger.error(
                "消费循环异常，即将重连",
                stream=stream,
                error=str(exc),
                messages_processed=message_count,
            )
            await asyncio.sleep(1)

    logger.info(
        "消费者正常退出",
        stream=stream,
        total_messages=message_count,
        total_errors=error_count,
    )


# ─── CLI 入口 ─────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="crypto-ai-trader Redis Stream 消费者",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--stream",
        type=str,
        required=True,
        help="要消费的 Redis Stream 名称",
    )
    parser.add_argument(
        "--group",
        type=str,
        default=None,
        help="消费者组名（默认使用 Stream 名称）",
    )
    parser.add_argument(
        "--consumer",
        type=str,
        default=None,
        help="消费者名（默认 auto-{pid}-{seq}）",
    )
    parser.add_argument(
        "--processor",
        type=str,
        default=None,
        help="处理器模块路径，格式: module.path:function_name",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI 入口。"""
    args = parse_args(argv)

    # 自动确定 group
    if args.group is None:
        args.group = args.stream

    # 加载处理器
    processor = None
    if args.processor:
        try:
            processor = _load_processor(args.processor)
        except (ImportError, AttributeError, TypeError) as exc:
            logger.error("处理器加载失败，退出", error=str(exc))
            sys.exit(1)

    try:
        asyncio.run(run_consumer(
            stream=args.stream,
            group=args.group,
            consumer=args.consumer,
            processor=processor,
        ))
    except KeyboardInterrupt:
        logger.info("消费者被用户中断")
    except Exception as exc:
        logger.error("消费者异常退出", error=str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
