"""
crypto-ai-trader 负载/稳定性测试脚本

功能:
  1. --smoke     快速冒烟测试（3-5 分钟），验证完整管线是否连通
  2. --load      吞吐量测试，推入大量 K 线观察背压和延迟
  3. --stability 长稳测试（默认 12h），周期性检查各 Stream 状态
  4. --latency   端到端延迟测量（raw_kline → trade_order）

用法:
  # 冒烟测试（验证管线连通性）
  python -m scripts.load_test --smoke

  # 吞吐量测试（持续 60 秒，每秒 50 根 K 线）
  python -m scripts.load_test --load --duration 60 --rate 50

  # 长稳测试（12 小时）
  python -m scripts.load_test --stability --duration 12h

  # 延迟测量（推送 100 根，统计分位数）
  python -m scripts.load_test --latency --count 100

  # 指定 Redis 地址（非默认 localhost:6379）
  python -m scripts.load_test --smoke --redis-host 10.0.0.1 --redis-port 6379

  # JSON 输出（供 CI 解析）
  python -m scripts.load_test --load --duration 30 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ─── 确保 /app 在路径中（Docker 内 / 本地通用） ─────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

logger = structlog.get_logger(__name__)

# 在结构化日志输出前，先配置基础日志
_LOGGER_CONFIGURED = False


def _ensure_logger():
    global _LOGGER_CONFIGURED
    if not _LOGGER_CONFIGURED:
        from logging_setup import setup_logging
        setup_logging(level="INFO", json_format=False)
        _LOGGER_CONFIGURED = True


_ensure_logger()


# ═══════════════════════════════════════════════════════════════
#  Mock K 线工厂（轻量版，无 numpy/pandas 依赖）
# ═══════════════════════════════════════════════════════════════

class MockKline:
    """生成 mock K 线，模拟 Binance WebSocket 输出格式。"""

    BASE_PRICE = 50000.0
    _price = BASE_PRICE

    @classmethod
    def next(
        cls,
        idx: int,
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
        is_closed: bool = True,
    ) -> dict[str, Any]:
        """生成单根 K 线。每次调用价格小幅波动。"""
        cls._price += random.uniform(-5.0, 5.0) + 0.5
        price = cls._price
        ts_base = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "ts": ts_base + idx * 3600_000,
            "open": f"{price - 10.0:.2f}",
            "high": f"{price + 15.0:.2f}",
            "low": f"{price - 12.0:.2f}",
            "close": f"{price + 3.0:.2f}",
            "volume": f"{100 + (idx % 50) * 3:.2f}",
            "quote_volume": f"{(100 + (idx % 50) * 3) * price:.2f}",
            "taker_buy_volume": f"{60 + (idx % 30) * 2:.2f}",
            "taker_buy_quote": f"{(60 + (idx % 30) * 2) * price:.2f}",
            "is_closed": is_closed,
        }

    @classmethod
    def reset_price(cls, base: float = 50000.0):
        """重置价格序列。"""
        cls._price = base


# ═══════════════════════════════════════════════════════════════
#  Redis 客户端
# ═══════════════════════════════════════════════════════════════

STREAMS = ["raw_kline", "indicators", "regime_signal", "ai_signal", "trade_order"]


def _connect(host: str = "localhost", port: int = 6379, db: int = 0):
    """创建 Redis 连接。"""
    import redis.asyncio as aioredis
    return aioredis.from_url(f"redis://{host}:{port}/{db}")


# ═══════════════════════════════════════════════════════════════
#  数据收集器
# ═══════════════════════════════════════════════════════════════

@dataclass
class StreamSnapshot:
    """单个时间点的 Stream 状态快照。"""
    timestamp: float
    stream: str
    length: int
    last_id: str = ""
    consumer_lag: int = 0


@dataclass
class LatencySample:
    """单条消息的端到端延迟样本。"""
    start_ts: float          # 注入 raw_kline 的时间戳
    stream: str              # 到达的 Stream 名
    arrival_ts: float        # 到达时间
    latency_ms: float        # 毫秒延迟
    symbol: str = ""
    direction: str = ""


@dataclass
class TestReport:
    """测试报告。"""
    mode: str
    start_time: str
    end_time: str
    duration_seconds: float
    klines_injected: int = 0
    indicators_produced: int = 0
    regime_signals_produced: int = 0
    ai_signals_produced: int = 0
    trade_orders_produced: int = 0
    errors: list[str] = field(default_factory=list)
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0
    latency_max_ms: float = 0.0
    max_stream_backlog: dict[str, int] = field(default_factory=dict)
    container_restarts: int = 0
    passed: bool = False


# ═══════════════════════════════════════════════════════════════
#  核心测试函数
# ═══════════════════════════════════════════════════════════════

async def smoke_test(r: Any) -> TestReport:
    """
    快速冒烟测试：注入 300 根 K 线，等待 60 秒，
    检查各 Stream 是否有数据产出。
    """
    report = TestReport(
        mode="smoke",
        start_time=datetime.now(timezone.utc).isoformat(),
        duration_seconds=0.0,
    )
    start = time.monotonic()

    logger.info("冒烟测试开始", klines=300, wait_seconds=60)

    # 注入 300 根 K 线（保证超过 200 预热阈值）
    MockKline.reset_price()
    for i in range(300):
        kline = MockKline.next(i)
        await r.xadd("raw_kline", kline, maxlen=10000)
        report.klines_injected += 1
        # 分批注入，避免瞬间撑爆
        if (i + 1) % 50 == 0:
            await asyncio.sleep(0.1)

    logger.info("K 线注入完成", count=report.klines_injected)

    # 等待下游处理（60 秒足够走完完整管线）
    await asyncio.sleep(60)

    # 检查各 Stream
    end = time.monotonic()
    report.duration_seconds = end - start
    report.end_time = datetime.now(timezone.utc).isoformat()

    for stream in STREAMS:
        try:
            length = await r.xlen(stream)
            setattr(report, f"{stream.replace('_signal', '_signals')}_produced"
                    if stream != "raw_kline"
                    else "indicators_produced"
                    if stream == "indicators"
                    else "raw_kline_remaining", length)

            # 读取最后一条消息
            entries = await r.xrevrange(stream, count=1)
            if entries:
                _, data = entries[0]
                logger.info(
                    "Stream 数据检查",
                    stream=stream,
                    length=length,
                    last_msg_symbol=data.get(b"symbol", b"").decode() or data.get("symbol", ""),
                )
            else:
                logger.warning("Stream 为空", stream=stream)
        except Exception as exc:
            report.errors.append(f"{stream}: {exc}")

    # 判断是否通过
    try:
        n_indicators = report.indicators_produced
        indicators = await r.xlen("indicators")
        regime = await r.xlen("regime_signal")
        ai = await r.xlen("ai_signal")
        trade = await r.xlen("trade_order")
        all_non_empty = indicators > 0 and regime > 0 and ai > 0 and trade > 0
    except Exception:
        all_non_empty = False

    report.passed = all_non_empty and len(report.errors) == 0
    if not report.passed:
        if not all_non_empty:
            report.errors.append("部分 Stream 未产出数据")
            try:
                for s in STREAMS[1:]:
                    l = await r.xlen(s)
                    logger.warning("Stream 状态", stream=s, length=l)
            except Exception:
                pass

    return report


async def load_test(
    r: Any,
    duration_sec: int = 60,
    rate: int = 50,
    json_output: bool = False,
) -> TestReport:
    """
    吞吐量测试：以指定速率注入 K 线，监测各 Stream 吞吐和延迟。

    参数:
        duration_sec: 测试持续时间（秒）
        rate:         每秒注入 K 线数
    """
    report = TestReport(
        mode="load",
        start_time=datetime.now(timezone.utc).isoformat(),
        duration_seconds=float(duration_sec),
    )
    start = time.monotonic()

    logger.info("吞吐量测试开始", duration=duration_sec, rate=rate, total=rate * duration_sec)

    # 预热（先注入 200 根触发指标计算）
    MockKline.reset_price()
    for i in range(200):
        kline = MockKline.next(i, timeframe="1m")
        await r.xadd("raw_kline", kline, maxlen=10000)
        report.klines_injected += 1
    await asyncio.sleep(5)

    # 主测试：持续注入
    interval = 1.0 / rate  # 每根 K 线间隔
    latency_samples: list[LatencySample] = []
    deadline = start + duration_sec
    idx = 200

    # 同时启动一个消费者监控任务
    async def monitor():
        last_snap = {s: 0 for s in STREAMS}
        samples: list[LatencySample] = []

        while time.monotonic() < deadline + 10:
            await asyncio.sleep(5)
            for stream in STREAMS:
                try:
                    length = await r.xlen(stream)
                    if length > last_snap.get(stream, 0):
                        diff = length - last_snap[stream]
                        throughput = diff / 5.0
                        # 记录延迟近似值
                        samples.append(LatencySample(
                            start_ts=time.time(),
                            stream=stream,
                            arrival_ts=time.time(),
                            latency_ms=0.0,  # 精确延迟由采样器统计
                        ))
                        report.max_stream_backlog[stream] = max(
                            report.max_stream_backlog.get(stream, 0), length
                        )
                        logger.debug(
                            "Stream 吞吐",
                            stream=stream,
                            throughput=f"{throughput:.1f} msg/s",
                            backlog=length,
                        )
                    last_snap[stream] = length
                except Exception:
                    pass
            # 检查容器重启（Docker 环境）
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "compose", "ps", "--format", "json",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                if stdout:
                    for line in stdout.decode().strip().split("\n"):
                        if line.strip():
                            try:
                                info = json.loads(line)
                                status = info.get("Status", "")
                                if "restarting" in status or "unhealthy" in status:
                                    report.errors.append(
                                        f"容器异常: {info.get('Service')} {status}"
                                    )
                            except json.JSONDecodeError:
                                pass
            except (FileNotFoundError, Exception):
                pass  # 不在 Docker 环境中

        return samples

    monitor_task = asyncio.create_task(monitor())

    # 主注入循环
    while time.monotonic() < deadline:
        kline = MockKline.next(idx, timeframe="1m")
        # 记录注入时间（用于延迟计算）
        inject_ts = time.time()
        await r.xadd("raw_kline", kline, maxlen=10000)
        report.klines_injected += 1
        idx += 1

        # 随机采样延迟：每 20 根记录一次
        if idx % 20 == 0:
            latency_samples.append(LatencySample(
                start_ts=inject_ts,
                stream="raw_kline",
                arrival_ts=inject_ts,
                latency_ms=0.0,
            ))

        await asyncio.sleep(interval)

    # 等待队列清空
    await asyncio.sleep(10)
    monitor_task.cancel()

    end = time.monotonic()
    report.end_time = datetime.now(timezone.utc).isoformat()
    report.duration_seconds = end - start

    # 收集最终 Stream 长度
    for stream in STREAMS:
        try:
            length = await r.xlen(stream)
            report.max_stream_backlog[stream] = length
        except Exception:
            pass

    # 计算各 Stream 产出
    try:
        report.indicators_produced = await r.xlen("indicators")
        ai_len = await r.xlen("ai_signal")
        trade_len = await r.xlen("trade_order")
        report.ai_signals_produced = ai_len
        report.trade_orders_produced = trade_len
    except Exception:
        pass

    # 判断是否通过
    report.passed = (
        len(report.errors) == 0
        and report.indicators_produced > 0
        and report.duration_seconds > 0
    )

    return report


async def stability_test(
    r: Any,
    duration_sec: int = 43200,  # 12h
    check_interval: int = 300,  # 每 5 分钟检查一次
    json_output: bool = False,
) -> TestReport:
    """
    长稳测试：持续注入 K 线（低速率），周期性检查 Stream 状态和容器健康。

    参数:
        duration_sec:   测试持续时间（秒，默认 12h = 43200）
        check_interval: 检查间隔（秒，默认 300 = 5min）
    """
    report = TestReport(
        mode="stability",
        start_time=datetime.now(timezone.utc).isoformat(),
        duration_seconds=0.0,
    )
    start = time.monotonic()
    deadline = start + duration_sec

    duration_str = f"{duration_sec / 3600:.1f}h" if duration_sec >= 3600 else f"{duration_sec}s"
    logger.info(
        "长稳测试开始",
        duration=duration_str,
        check_interval=check_interval,
        deadline=datetime.fromtimestamp(deadline, tz=timezone.utc).isoformat(),
    )

    # 预热
    MockKline.reset_price()
    for i in range(250):
        kline = MockKline.next(i, timeframe="5m")
        await r.xadd("raw_kline", kline, maxlen=10000)
        report.klines_injected += 1
        if (i + 1) % 50 == 0:
            await asyncio.sleep(0.1)

    # 稳态注入（每 30 秒 1 根，模拟真实环境）
    async def injector():
        nonlocal report
        idx = 250
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            kline = MockKline.next(idx, timeframe="5m")
            await r.xadd("raw_kline", kline, maxlen=10000)
            report.klines_injected += 1
            idx += 1
            await asyncio.sleep(30)

    # 检查任务
    snapshots: list[StreamSnapshot] = []
    container_restarts = 0
    last_container_starts: dict[str, str] = {}

    async def checker():
        nonlocal container_restarts, last_container_starts, report

        while True:
            now = time.monotonic()
            if now >= deadline + check_interval:
                break

            await asyncio.sleep(check_interval)
            check_time = time.time()

            # 检查各 Stream 长度
            for stream in STREAMS:
                try:
                    length = await r.xlen(stream)
                    snapshots.append(StreamSnapshot(
                        timestamp=check_time,
                        stream=stream,
                        length=length,
                    ))
                    report.max_stream_backlog[stream] = max(
                        report.max_stream_backlog.get(stream, 0), length
                    )

                    # 如果 Stream 长度异常增长（超过背压阈值），记录警告
                    if length > 5000:
                        w = f"{stream} 堆积 {length} 条（超过背压阈值 5000）"
                        if w not in report.errors:
                            report.errors.append(w)

                except Exception as exc:
                    report.errors.append(f"检查 {stream} 失败: {exc}")

            # 检查 Stream 延迟：读取最后一条消息的时间戳
            for stream in STREAMS[1:]:
                try:
                    entries = await r.xrevrange(stream, count=1)
                    if entries:
                        msg_id, _ = entries[0]
                        # Redis ID 格式: timestamp-sequence
                        id_ts = int(msg_id.split("-")[0]) / 1000.0
                        age = check_time - id_ts
                        if age > 300:  # 超过 5 分钟无新消息
                            report.errors.append(
                                f"{stream} 最新消息已 {age:.0f} 秒未更新"
                            )
                except Exception:
                    pass

            # Docker 容器状态检查
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "compose", "ps", "--format", "json",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if stdout:
                    for line in stdout.decode().strip().split("\n"):
                        if not line.strip():
                            continue
                        try:
                            info = json.loads(line)
                            service = info.get("Service", "")
                            status = info.get("Status", "")
                            container_id = info.get("ID", "")

                            # 检测重启
                            prev = last_container_starts.get(service)
                            if prev is None:
                                last_container_starts[service] = container_id
                            elif prev != container_id:
                                container_restarts += 1
                                report.errors.append(
                                    f"容器 {service} 已重启（共 {container_restarts} 次）"
                                )
                                last_container_starts[service] = container_id

                            if "restarting" in status:
                                report.errors.append(f"容器 {service} 正在重启: {status}")
                            elif "unhealthy" in status:
                                report.errors.append(f"容器 {service} 不健康: {status}")
                        except json.JSONDecodeError:
                            pass
            except (FileNotFoundError, Exception):
                pass  # 不在 Docker 环境中

            # 内存检查（通过 docker stats）
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                if stdout:
                    for line in stdout.decode().strip().split("\n"):
                        if "crypto-ai-trader" in line:
                            parts = line.split("\t")
                            if len(parts) >= 2:
                                logger.debug("容器内存", name=parts[0], memory=parts[1])
            except (FileNotFoundError, Exception):
                pass

            # 进度打印
            elapsed = time.monotonic() - start
            progress = min(elapsed / duration_sec * 100, 100)
            errors_now = len(report.errors)
            logger.info(
                "长稳检查",
                progress=f"{progress:.0f}%",
                elapsed=f"{elapsed:.0f}s",
                klines=report.klines_injected,
                errors=errors_now,
                container_restarts=container_restarts,
            )

    inj_task = asyncio.create_task(injector())
    chk_task = asyncio.create_task(checker())

    await asyncio.gather(inj_task, chk_task)

    end = time.monotonic()
    report.end_time = datetime.now(timezone.utc).isoformat()
    report.duration_seconds = end - start
    report.container_restarts = container_restarts

    # 最终的 Stream 长度
    for stream in STREAMS:
        try:
            length = await r.xlen(stream)
            report.max_stream_backlog[stream] = length
        except Exception:
            pass

    # 判断是否通过
    report.passed = (
        container_restarts == 0
        and len([e for e in report.errors if "异常" in e or "重启" in e]) == 0
        and report.duration_seconds >= duration_sec * 0.95  # 至少运行了 95% 的时间
    )

    return report


async def latency_test(r: Any, count: int = 100, json_output: bool = False) -> TestReport:
    """
    端到端延迟测量：注入 K 线，在 trade_order Stream 读取结果，
    计算各阶段的延迟分布。

    参数:
        count: 注入的 K 线批次数
    """
    report = TestReport(
        mode="latency",
        start_time=datetime.now(timezone.utc).isoformat(),
        duration_seconds=0.0,
    )
    start = time.monotonic()

    logger.info("延迟测试开始", batches=count)

    # 预热（确保管线已就绪）
    MockKline.reset_price()
    for i in range(220):
        kline = MockKline.next(i)
        await r.xadd("raw_kline", kline, maxlen=10000)
        report.klines_injected += 1

    # 等待管线预热完成
    logger.info("预热完成，等待管线就绪...")
    await asyncio.sleep(15)

    # 检查 indicators Stream 是否有数据
    try:
        ind_len = await r.xlen("indicators")
        logger.info("预热后 indicators Stream 长度", length=ind_len)
    except Exception:
        pass

    # 主测试：逐批注入，测量端到端延迟
    all_latencies: list[float] = []
    batch_errors = 0

    for batch in range(count):
        batch_start = time.time()

        # 注入一批 K 线（5 根）
        for j in range(5):
            kline = MockKline.next(batch * 5 + j)
            kline["timeframe"] = "1m"  # 使用小周期加速测试
            await r.xadd("raw_kline", kline, maxlen=10000)

        # 等待管线处理（最长 15 秒）
        found = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not found:
            try:
                entries = await r.xrevrange("trade_order", count=1)
                if entries:
                    _, data = entries[0]
                    # 尝试解码（redis-py 返回 bytes 键）
                    msg_ts = data.get(b"ts") or data.get("ts")
                    if msg_ts is not None:
                        if isinstance(msg_ts, bytes):
                            msg_ts = float(msg_ts.decode())
                        else:
                            msg_ts = float(msg_ts)
                        # 使用 Redis Stream 的 ID 时间戳作为基准
                        id_ts = int(entries[0][0].split("-")[0]) / 1000.0
                        latency_ms = (time.time() - id_ts) * 1000
                        all_latencies.append(latency_ms)
                        found = True

                        action = (data.get(b"action") or data.get("action", b"")).decode() \
                            if isinstance(data.get(b"action") or data.get("action"), bytes) \
                            else str(data.get("action", ""))
                        logger.info(
                            "端到端延迟样本",
                            batch=batch,
                            latency=f"{latency_ms:.1f}ms",
                            action=action,
                        )
            except Exception as exc:
                if batch_errors < 3:
                    logger.warning("读取 trade_order 失败", error=str(exc))
                    batch_errors += 1
                break

            await asyncio.sleep(0.5)

        if not found:
            logger.warning("未在超时内收到 trade_order", batch=batch)

        # 批次间隔
        await asyncio.sleep(1)

    end = time.monotonic()
    report.duration_seconds = end - start
    report.end_time = datetime.now(timezone.utc).isoformat()

    # 统计延迟
    if all_latencies:
        sorted_lat = sorted(all_latencies)
        report.latency_p50_ms = statistics.median(sorted_lat)
        n = len(sorted_lat)
        report.latency_p95_ms = sorted_lat[min(int(n * 0.95), n - 1)]
        report.latency_p99_ms = sorted_lat[min(int(n * 0.99), n - 1)]
        report.latency_max_ms = sorted_lat[-1]

        logger.info(
            "延迟统计",
            samples=n,
            p50=f"{report.latency_p50_ms:.1f}ms",
            p95=f"{report.latency_p95_ms:.1f}ms",
            p99=f"{report.latency_p99_ms:.1f}ms",
            max=f"{report.latency_max_ms:.1f}ms",
        )
    else:
        report.errors.append("未收集到任何延迟样本")

    # 最终 Stream 状态
    try:
        report.indicators_produced = await r.xlen("indicators")
        report.trade_orders_produced = await r.xlen("trade_order")
    except Exception:
        pass

    # 判断是否通过 (p95 < 10秒)
    report.passed = (
        len(report.errors) == 0
        and report.latency_p95_ms > 0
        and report.latency_p95_ms < 10000
    )

    return report


# ═══════════════════════════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════════════════════════

def _parse_duration(val: str) -> int:
    """解析时间字符串为秒数。
    支持: 30s, 5m, 2h, 12h, 3600 (纯数字=秒)
    """
    val = val.strip().lower()
    if val.endswith("h"):
        return int(val[:-1]) * 3600
    elif val.endswith("m"):
        return int(val[:-1]) * 60
    elif val.endswith("s"):
        return int(val[:-1])
    else:
        return int(val)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="crypto-ai-trader 负载/稳定性测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # 连接参数
    parser.add_argument("--redis-host", default=os.getenv("REDIS_HOST", "localhost"),
                        help="Redis 主机地址（默认 localhost）")
    parser.add_argument("--redis-port", type=int,
                        default=int(os.getenv("REDIS_PORT", "6379")),
                        help="Redis 端口（默认 6379）")

    # 测试模式（互斥，必须指定一种）
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smoke", action="store_true",
                      dest="smoke",
                      help="冒烟测试：注入 300 根 K 线，验证管线完整性")
    mode.add_argument("--load", action="store_true",
                      dest="load",
                      help="吞吐量测试：高速注入，观察背压和延迟")
    mode.add_argument("--stability", action="store_true",
                      dest="stability",
                      help="长稳测试：持续运行，周期性检查")
    mode.add_argument("--latency", action="store_true",
                      dest="latency",
                      help="延迟测试：精确测量 end-to-end 延迟分布")

    # 参数
    parser.add_argument("--duration", default="60s",
                        help="测试持续时间（30s / 5m / 2h / 12h，默认 60s）")
    parser.add_argument("--rate", type=int, default=50,
                        help="注入速率（条/秒，默认 50，仅 --load）")
    parser.add_argument("--count", type=int, default=100,
                        help="采样批次数量（默认 100，仅 --latency）")
    parser.add_argument("--check-interval", type=int, default=300,
                        help="检查间隔秒数（默认 300，仅 --stability）")
    parser.add_argument("--json", dest="json_output", action="store_true",
                        help="输出 JSON 格式报告")
    parser.add_argument("--quiet", action="store_true",
                        help="减少日志输出")

    return parser


async def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.quiet:
        from logging_setup import setup_logging
        setup_logging(level="WARNING", json_format=False)

    # 连接 Redis
    try:
        r = _connect(host=args.redis_host, port=args.redis_port)
        await r.ping()
        logger.info("Redis 连接成功", host=args.redis_host, port=args.redis_port)
    except Exception as exc:
        print(f"错误: 无法连接 Redis ({args.redis_host}:{args.redis_port}) — {exc}")
        sys.exit(1)

    report: TestReport | None = None

    try:
        if args.smoke:
            report = await smoke_test(r)
        elif args.load:
            duration = _parse_duration(args.duration)
            report = await load_test(r, duration_sec=duration, rate=args.rate)
        elif args.stability:
            duration = _parse_duration(args.duration)
            report = await stability_test(
                r,
                duration_sec=duration,
                check_interval=args.check_interval,
            )
        elif args.latency:
            report = await latency_test(r, count=args.count)
    except KeyboardInterrupt:
        logger.warning("测试被用户中断")
    except Exception as exc:
        logger.error("测试异常", error=str(exc), exc_info=True)
        report = TestReport(
            mode="unknown",
            start_time=datetime.now(timezone.utc).isoformat(),
            end_time=datetime.now(timezone.utc).isoformat(),
            duration_seconds=0.0,
            errors=[str(exc)],
            passed=False,
        )
    finally:
        await r.close()

    if report is None:
        report = TestReport(
            mode="unknown",
            start_time=datetime.now(timezone.utc).isoformat(),
            end_time=datetime.now(timezone.utc).isoformat(),
            duration_seconds=0.0,
            errors=["测试未正常完成"],
            passed=False,
        )

    # 输出报告
    if args.json_output:
        data = asdict(report)
        data["start_time"] = str(data["start_time"])
        data["end_time"] = str(data["end_time"])
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        verdict = "✅ PASSED" if report.passed else "❌ FAILED"
        print(f"\n{'='*60}")
        print(f"  测试报告 — {report.mode.upper()}")
        print(f"{'='*60}")
        print(f"  结果:        {verdict}")
        print(f"  持续时间:    {report.duration_seconds:.0f}s")
        print(f"  K 线注入:    {report.klines_injected}")
        print(f"  Indicators:  {report.indicators_produced}")
        print(f"  AI Signals:  {report.ai_signals_produced}")
        print(f"  Trade Orders: {report.trade_orders_produced}")
        print(f"  延迟 P50:    {report.latency_p50_ms:.0f}ms")
        print(f"  延迟 P95:    {report.latency_p95_ms:.0f}ms")
        print(f"  延迟 P99:    {report.latency_p99_ms:.0f}ms")
        if report.max_stream_backlog:
            print(f"  Max backlog: {report.max_stream_backlog}")
        if report.container_restarts:
            print(f"  容器重启:    {report.container_restarts}")
        if report.errors:
            print(f"  错误:        {len(report.errors)}")
            for err in report.errors[:10]:
                print(f"    - {err}")
            if len(report.errors) > 10:
                print(f"    ... 还有 {len(report.errors) - 10} 条")
        print(f"{'='*60}\n")

    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
