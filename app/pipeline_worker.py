"""
模块名称: pipeline_worker.py
所属层级: 基础设施层 (Infrastructure) — 合并流水线 Worker
输入来源: Binance WebSocket / Redis Stream
输出去向: Redis Stream + SQLite（决策日志）+ Parquet（历史 K 线）+ 通知
关键依赖: structlog, redis, aiosqlite, pandas

低配硬件优化版 —— 将 5 个独立 worker 合并为单进程运行。
一个容器完成: 数据采集 → 指标计算 → 制度识别 → AI 引擎 → 风控审核。
相比独立容器模式节省 ~300-400MB Python 进程开销。

所有业务模块（indicators/、regime/、ai_engine/、risk_guardian/）保持不动，
此文件仅作编排。通知（Telegram/钉钉）复用 observability/alert_manager.py，
作为内部脚本运行，不另开容器。

铁律:
    - 所有阶段间通信通过 Redis Stream（ARCH.md #7），禁止直接函数调用
    - LLM 输出必须经 schema_validator 校验后才能流转（ARCH.md #5）
    - 密钥从 .env / secrets/ 加载，不出现在日志中（ARCH.md #4）
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timezone
from typing import Any, Coroutine

import structlog


# ─── 必须在所有 import 完成前初始化日志 ───────────────────────
from logging_setup import setup_logging

setup_logging(
    level=os.getenv("LOG_LEVEL", "INFO"),
    json_format=os.getenv("LOG_JSON", "").lower() in ("1", "true", "yes"),
)

logger = structlog.get_logger(__name__)


# ─── 信号处理 ─────────────────────────────────────────────────

_shutdown_event = asyncio.Event()


def _handle_signal(sig: int, frame) -> None:
    signal_name = signal.Signals(sig).name
    logger.info("收到退出信号", signal=signal_name)
    _shutdown_event.set()


# ─── SQLite 决策日志器（轻量替代 TimescaleDB decision_log 超表）─

class SQLiteDecisionLogger:
    """
    基于 SQLite 的决策日志器。
    替代 observability/decision_logger.py（基于 asyncpg/TimescaleDB）。
    单文件存储，零守护进程，零内存开销。
    """

    DB_PATH = os.getenv("LITE_DB_PATH", "data/decisions.db")

    def __init__(self) -> None:
        self._conn: Any = None

    async def connect(self) -> None:
        try:
            aiosqlite = __import__("aiosqlite")
            os.makedirs(os.path.dirname(self.DB_PATH), exist_ok=True)
            self._conn = await aiosqlite.connect(self.DB_PATH)
            await self._conn.execute("""
                CREATE TABLE IF NOT EXISTS decisions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          TEXT NOT NULL,
                    symbol      TEXT NOT NULL,
                    direction   TEXT,
                    confidence  REAL,
                    regime      TEXT,
                    score       REAL,
                    reasoning   TEXT,
                    is_fallback INTEGER DEFAULT 0,
                    created_at  TEXT DEFAULT (datetime('now'))
                )
            """)
            await self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_decisions_ts
                ON decisions(ts DESC)
            """)
            await self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_decisions_symbol
                ON decisions(symbol)
            """)
            await self._conn.commit()
            logger.info("SQLite 决策日志已就绪", path=self.DB_PATH)
        except ImportError:
            logger.warning("aiosqlite 未安装，决策日志仅写入 structlog")
            self._conn = None
        except Exception as exc:
            logger.error("SQLite 初始化失败，降级为仅日志", error=str(exc))
            self._conn = None

    async def log_decision(self, **kwargs: Any) -> None:
        """记录一次决策到 SQLite + structlog。"""
        symbol = kwargs.get("symbol", "UNKNOWN")
        direction = kwargs.get("direction")
        confidence = kwargs.get("confidence")
        regime = kwargs.get("regime")
        score = kwargs.get("score")

        logger.info(
            "DECISION",
            symbol=symbol,
            direction=direction,
            confidence=confidence,
            regime=regime,
            score=score,
        )

        if self._conn is None:
            return

        try:
            await self._conn.execute(
                """INSERT INTO decisions (ts, symbol, direction, confidence, regime, score, reasoning, is_fallback)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    symbol,
                    direction,
                    confidence,
                    regime,
                    score,
                    kwargs.get("reasoning", ""),
                    1 if kwargs.get("is_fallback") else 0,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            logger.error("写入 SQLite 决策日志失败", error=str(exc))

    async def fetch_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """查询最近的决策记录。"""
        if self._conn is None:
            return []
        try:
            self._conn.row_factory = aiosqlite.Row  # type: ignore[attr-defined]
            cursor = await self._conn.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            logger.error("查询 SQLite 决策日志失败", error=str(exc))
            return []

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("SQLite 连接已关闭")


# ─── 通知集成（复用 observability/alert_manager.py）───────────

class NotificationManager:
    """
    通知管理器。
    集成 Telegram / 钉钉通知，作为 pipeline-worker 内部脚本运行。
    复用 observability/alert_manager.py 的现有通道实现。
    """

    def __init__(self) -> None:
        self._telegram_token = os.getenv("ALERT_TELEGRAM_BOT_TOKEN", "")
        self._telegram_chat_id = os.getenv("ALERT_TELEGRAM_CHAT_ID", "")
        self._dingtalk_webhook = os.getenv("ALERT_DINGTALK_WEBHOOK", "")
        self._dingtalk_secret = os.getenv("ALERT_DINGTALK_SECRET", "")
        self._alert_manager: Any = None
        self._AlertLevel: Any = None
        self._initialized = False

    async def initialize(self) -> None:
        """初始化通知通道（Telegram/钉钉）。"""
        try:
            from observability.alert_manager import (
                AlertManager, AlertLevel, get_alert_manager,
            )
            self._alert_manager = get_alert_manager()
            self._AlertLevel = AlertLevel

            if self._telegram_token and self._telegram_chat_id:
                from observability.alert_manager import TelegramChannel
                tg = TelegramChannel(
                    bot_token=self._telegram_token,
                    chat_id=self._telegram_chat_id,
                )
                self._alert_manager.add_channel(tg)
                logger.info("Telegram 通知已配置")

            if self._dingtalk_webhook:
                from observability.alert_manager import DingTalkChannel
                dt = DingTalkChannel(
                    webhook_url=self._dingtalk_webhook,
                    secret=self._dingtalk_secret or None,
                )
                self._alert_manager.add_channel(dt)
                logger.info("钉钉通知已配置")

            self._initialized = True
        except ImportError:
            logger.debug("alert_manager 未安装或通知库缺失")
        except Exception as exc:
            logger.warning("通知初始化失败", error=str(exc))

    async def notify_trade(self, **kwargs: Any) -> None:
        """发送交易信号通知。"""
        if not self._initialized:
            return
        try:
            symbol = kwargs.get("symbol", "UNKNOWN")
            action = kwargs.get("action", "NONE")
            msg = self._alert_manager.create_message(
                level=self._AlertLevel.INFO,
                title=f"交易信号: {action} {symbol}",
                detail=(
                    f"仓位: {kwargs.get('size_pct', 0)*100:.1f}% | "
                    f"置信度: {kwargs.get('confidence', 0):.2f} | "
                    f"制度: {kwargs.get('regime', 'UNKNOWN')}"
                ),
                symbol=symbol,
            )
            await self._alert_manager.send_message(msg)
        except Exception as exc:
            logger.warning("发送交易通知失败", error=str(exc))

    async def notify_alert(self, level: str, title: str, detail: str = "") -> None:
        """发送告警通知。"""
        if not self._initialized:
            return
        try:
            level_map = {
                "info": self._AlertLevel.INFO,
                "warning": self._AlertLevel.WARNING,
                "critical": self._AlertLevel.CRITICAL,
            }
            msg = self._alert_manager.create_message(
                level=level_map.get(level, self._AlertLevel.INFO),
                title=title, detail=detail,
            )
            await self._alert_manager.send_message(msg)
        except Exception as exc:
            logger.warning("发送告警失败", error=str(exc))

    async def close(self) -> None:
        pass


# ─── 合并流水线 Worker ──────────────────────────────────────

class PipelineWorker:
    """
    合并流水线 Worker。
    在单进程中运行所有 5 个处理阶段，通过 Redis Stream 通信。

    阶段:
        1. raw_kline  ← 数据采集（Binance WebSocket）
        2. indicators ← 指标计算（消费 raw_kline）
        3. regime_signal ← 制度识别（消费 indicators）
        4. ai_signal     ← AI 引擎（消费 regime_signal）
        5. trade_order   ← 风控审核（消费 ai_signal）
    """

    def __init__(self) -> None:
        self._redis: Any = None
        self._decision_logger = SQLiteDecisionLogger()
        self._notifier = NotificationManager()
        self._kline_store: Any = None
        self._should_run_data_collector = (
            os.getenv("DISABLE_DATA_COLLECTOR", "").lower() not in ("1", "true", "yes")
        )
        self._tasks: list[asyncio.Task] = []

    # ── Redis 连接 ──────────────────────────────────────

    async def _connect_redis(self) -> None:
        redis_host = os.getenv("REDIS_HOST", "redis")
        redis_port = int(os.getenv("REDIS_PORT", "6379"))
        redis = __import__("redis")
        self._redis = redis.asyncio.from_url(
            f"redis://{redis_host}:{redis_port}",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_keepalive=True,
        )
        await self._redis.ping()
        logger.info("Redis 连接就绪", host=redis_host, port=redis_port)

    # ── 数据采集阶段 ────────────────────────────────────

    async def _run_collector(self) -> None:
        """Binance WebSocket 数据采集。"""
        try:
            from data.ws_client import BinanceWSClient

            symbols_str = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT")
            symbols = [s.strip().upper() for s in symbols_str.split(",") if s.strip()]
            interval = os.getenv("KLINE_INTERVAL", "1m")

            logger.info("启动数据采集", symbols=symbols, interval=interval)
            client = BinanceWSClient(symbols=symbols, interval=interval)
            await client.run()
        except ImportError:
            logger.warning("BinanceWSClient 导入失败，跳过数据采集")
        except Exception:
            logger.exception("数据采集异常退出")

    # ── 指标计算阶段 ────────────────────────────────────

    async def _run_indicator(self) -> None:
        """消费 raw_kline → 计算指标 → 写入 indicators Stream。"""
        from messaging.consumer import run_consumer
        from indicators.processor import process_raw_kline

        logger.info("启动指标计算")
        await run_consumer(
            stream="raw_kline",
            group="indicators",
            consumer=f"pipeline-indicator-{os.getpid()}",
            processor=process_raw_kline,
        )

    # ── 制度识别阶段 ────────────────────────────────────

    async def _run_regime(self) -> None:
        """消费 indicators → 制度检测 → 写入 regime_signal Stream。"""
        from messaging.consumer import run_consumer
        from regime.processor import process_indicators

        logger.info("启动制度识别")
        await run_consumer(
            stream="indicators",
            group="regime",
            consumer=f"pipeline-regime-{os.getpid()}",
            processor=process_indicators,
        )

    # ── AI 引擎阶段 ─────────────────────────────────────

    async def _run_ai_engine(self) -> None:
        """消费 regime_signal → PlanGenerator → 写入 ai_signal Stream。"""
        from messaging.consumer import run_consumer
        from ai_engine.processor import process_regime_signal

        logger.info("启动 AI 引擎")
        await run_consumer(
            stream="regime_signal",
            group="ai_engine",
            consumer=f"pipeline-ai-{os.getpid()}",
            processor=process_regime_signal,
        )

    # ── 阶段 5: 风控审核 ────────────────────────────────

    async def _run_risk(self) -> None:
        """消费 ai_signal → 风险审核 → 写入 trade_order Stream。
        复用 risk_guardian/processor.py。

        同时:
        - 写入 SQLite 决策日志
        - 发送 Telegram/钉钉通知
        - Freqtrade 消费 trade_order 执行下单
        """
        from messaging.consumer import run_consumer
        from risk_guardian.processor import process_ai_signal

        original_process = process_ai_signal

        async def logged_process(message: dict[str, Any]) -> dict[str, Any] | None:
            result = await original_process(message)
            if result and result.get("action") in ("LONG", "SHORT", "FORCE_EXIT"):
                # 决策日志
                await self._decision_logger.log_decision(
                    symbol=result.get("symbol", message.get("symbol", "UNKNOWN")),
                    direction=result.get("action"),
                    confidence=message.get("confidence", 0),
                    regime=message.get("regime", "UNKNOWN"),
                    score=message.get("score", 0),
                    reasoning=f"trade_order: {json.dumps(result)}",
                    is_fallback=message.get("is_fallback", False),
                )
                # 通知
                await self._notifier.notify_trade(
                    symbol=result.get("symbol", message.get("symbol", "UNKNOWN")),
                    action=result.get("action"),
                    size_pct=result.get("size_pct", 0),
                    confidence=message.get("confidence", 0),
                    regime=message.get("regime", "UNKNOWN"),
                )
            return result

        logger.info("启动风控审核")
        await run_consumer(
            stream="ai_signal",
            group="risk_guardian",
            consumer=f"pipeline-risk-{os.getpid()}",
            processor=logged_process,
        )

    # ── 启动所有阶段 ────────────────────────────────────

    async def run(self) -> None:
        """启动所有流水线阶段。"""
        # 1. 连接基础服务
        await self._connect_redis()
        await self._decision_logger.connect()
        await self._notifier.initialize()

        # 2. 初始化 KlineStore（Parquet 历史 K 线存储）
        try:
            from storage.historical_store import KlineStore
            self._kline_store = KlineStore()
        except ImportError:
            logger.info("KlineStore 不可用，历史 K 线存储跳过")
        except PermissionError:
            logger.warning("data/ 目录无写入权限，KlineStore 跳过")
        except OSError as exc:
            logger.warning("KlineStore 初始化失败", error=str(exc))

        # 3. 启动 5 个流水线阶段
        stage_tasks: list[tuple[str, Coroutine]] = [
            ("indicators", self._run_indicator()),
            ("regime", self._run_regime()),
            ("ai_engine", self._run_ai_engine()),
            ("risk", self._run_risk()),
        ]

        if self._should_run_data_collector:
            stage_tasks.insert(0, ("data_collector", self._run_collector()))

        for name, coro in stage_tasks:
            task = asyncio.create_task(coro, name=name)
            self._tasks.append(task)
            logger.info("流水线阶段已启动", stage=name)

        # 3. 等待退出信号
        await _shutdown_event.wait()
        logger.info("收到退出信号，正在停止所有阶段...")

        # 4. 优雅停止
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        await self._decision_logger.close()
        await self._notifier.close()
        if self._kline_store:
            self._kline_store.close()
        if self._redis:
            await self._redis.close()
        logger.info("流水线 Worker 已优雅退出")


# ─── 轻量健康检查端点（替代 scripts/health_check.py）───────

async def run_health_check() -> None:
    """简单的存活检查。"""
    redis_host = os.getenv("REDIS_HOST", "redis")
    redis_port = int(os.getenv("REDIS_PORT", "6379"))

    checks: dict[str, bool] = {}

    # Redis 检查
    try:
        redis = __import__("redis")
        r = redis.asyncio.from_url(
            f"redis://{redis_host}:{redis_port}", decode_responses=True
        )
        await r.ping()
        await r.close()
        checks["redis"] = True
    except Exception:
        checks["redis"] = False

    # SQLite 检查
    try:
        db_path = os.getenv("LITE_DB_PATH", "data/decisions.db")
        checks["sqlite"] = os.path.exists(db_path)
    except Exception:
        checks["sqlite"] = False

    all_ok = all(checks.values())
    result = {
        "status": "ok" if all_ok else "degraded",
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(result))
    sys.exit(0 if all_ok else 1)


# ─── 入口 ────────────────────────────────────────────────────

async def main() -> None:
    """入口：启动流水线 Worker 或健康检查。"""
    # 注册信号处理
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig, None)
        except NotImplementedError:
            # Windows 不支持 add_signal_handler
            pass

    if "--health-check" in sys.argv:
        await run_health_check()
        return

    logger.info(
        "Pipeline Worker 启动（低配优化模式）",
        pid=os.getpid(),
        stages="data→indicators→regime→ai→risk→freqtrade",
    )

    worker = PipelineWorker()
    await worker.run()


def entry_point() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
