"""
模块名称: decision_logger.py
所属层级: 可观测性层 (Observability)
输入来源: plan_generator / signal_scorer / fallback_handler 等 AI 决策环节
输出去向: TimescaleDB decision_log 超表 + structlog 控制台
关键依赖: asyncpg · structlog

决策链路日志。
记录每次 AI 决策的完整上下文，写入 TimescaleDB decision_log 超表。
DB 不可用时静默降级为仅控制台日志。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ─── DB 配置 ─────────────────────────────────────

_DB_CONFIG: dict[str, Any] = {
    "host": "localhost",
    "port": 5432,
    "user": "trader",
    "password": "trader",
    "database": "crypto_trader",
    "min_size": 1,
    "max_size": 5,
}


# ─── 决策记录模型 ────────────────────────────────

@dataclass
class DecisionRecord:
    """单次决策的完整记录。"""
    ts: str
    symbol: str
    timeframe: str
    prompt_version: str
    regime: str
    raw_llm_output: str
    validated: bool
    direction: str | None
    confidence: float | None
    breaker_state: str
    signal_sent: bool

    @classmethod
    def from_plan(
        cls,
        *,
        symbol: str,
        timeframe: str,
        prompt_version: str,
        regime: str,
        raw_llm_output: str,
        validated: bool,
        direction: str | None,
        confidence: float | None,
        breaker_state: str = "CLOSED",
        signal_sent: bool = False,
    ) -> DecisionRecord:
        """从 TradePlan / 回传参数构造记录，自动填入当前时间戳。"""
        return cls(
            ts=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            timeframe=timeframe,
            prompt_version=prompt_version,
            regime=regime,
            raw_llm_output=raw_llm_output,
            validated=validated,
            direction=direction,
            confidence=confidence,
            breaker_state=breaker_state,
            signal_sent=signal_sent,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── decision_log 超表列名映射 ──────────────────

_DB_COLUMNS = [
    "ts",
    "symbol",
    "timeframe",
    "prompt_version",
    "regime",
    "validated",
    "direction",
    "confidence",
    "breaker_state",
    "signal_sent",
    "raw_output",
]

_INSERT_SQL = (
    "INSERT INTO decision_log ("
    + ", ".join(_DB_COLUMNS)
    + ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)"
)


# ─── 决策日志器 ──────────────────────────────────

class DecisionLogger:
    """
    决策链路日志器。

    用法:
        logger = DecisionLogger()
        await logger.connect()  # 可选：不调用则仅日志
        record = DecisionRecord.from_plan(...)
        await logger.log(record)
        await logger.close()
    """

    def __init__(self, db_config: dict[str, Any] | None = None) -> None:
        self._pool: Any = None
        self._db_config = {**_DB_CONFIG, **(db_config or {})}
        self._asyncpg_available = False

        # 检查 asyncpg 是否可用
        try:
            __import__("asyncpg")
            self._asyncpg_available = True
        except ImportError:
            logger.warning("asyncpg 未安装，仅输出控制台日志")

    # ── 连接池管理 ─────────────────────────────

    async def connect(self) -> None:
        """创建数据库连接池。"""
        if not self._asyncpg_available:
            return
        if self._pool is not None:
            return
        try:
            asyncpg = __import__("asyncpg")
            self._pool = await asyncpg.create_pool(
                host=self._db_config["host"],
                port=self._db_config["port"],
                user=self._db_config["user"],
                password=self._db_config["password"],
                database=self._db_config["database"],
                min_size=self._db_config["min_size"],
                max_size=self._db_config["max_size"],
            )
            logger.info("TimescaleDB 连接池已创建")
        except Exception as exc:
            logger.error("TimescaleDB 连接失败，降级为仅日志", error=str(exc))
            self._pool = None

    async def close(self) -> None:
        """关闭连接池。"""
        if self._pool is not None:
            try:
                await self._pool.close()
                self._pool = None
                logger.info("TimescaleDB 连接池已关闭")
            except Exception as exc:
                logger.error("关闭 TimescaleDB 连接池失败", error=str(exc))

    @property
    def connected(self) -> bool:
        return self._pool is not None

    # ── 写入 ───────────────────────────────────

    async def log(self, record: DecisionRecord) -> None:
        """
        记录一次决策。

        同时输出到 structlog 控制台 +（如有连接）TimescaleDB。
        """
        logger.info(
            "DECISION",
            ts=record.ts,
            symbol=record.symbol,
            timeframe=record.timeframe,
            validated=record.validated,
            direction=record.direction,
            confidence=record.confidence,
            breaker_state=record.breaker_state,
            signal_sent=record.signal_sent,
        )

        # 同时输出完整 JSON 方便 ELK / 文件日志
        logger.debug("DECISION_RAW", payload=record.to_dict())

        if self._pool is not None:
            await self._write_db(record)

    async def _write_db(self, record: DecisionRecord) -> None:
        """将记录写入 TimescaleDB decision_log 超表。"""
        if self._pool is None:
            return

        params = (
            record.ts,
            record.symbol,
            record.timeframe,
            record.prompt_version,
            record.regime,
            record.validated,
            record.direction,
            record.confidence,
            record.breaker_state,
            record.signal_sent,
            record.raw_llm_output,
        )

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(_INSERT_SQL, *params)
        except Exception as exc:
            logger.error("写入 decision_log 失败", error=str(exc))

    # ── 查询（只读）────────────────────────────

    async def fetch_recent(
        self,
        limit: int = 20,
        symbol: str | None = None,
        validated_only: bool = False,
    ) -> list[dict[str, Any]]:
        """
        查询最近的决策记录。

        参数:
            limit: 返回条数
            symbol: 可选，按交易对筛选
            validated_only: 仅返回校验通过的决策

        返回:
            dict 列表，按时间降序
        """
        if self._pool is None:
            logger.warning("DB 未连接，无法查询")
            return []

        try:
            conditions: list[str] = []
            params: list[Any] = []
            idx = 1

            if symbol:
                conditions.append(f"symbol = ${idx}")
                params.append(symbol)
                idx += 1

            if validated_only:
                conditions.append(f"validated = ${idx}")
                params.append(True)
                idx += 1

            where = ""
            if conditions:
                where = "WHERE " + " AND ".join(conditions)

            sql = (
                f"SELECT * FROM decision_log {where} "
                f"ORDER BY ts DESC LIMIT ${idx}"
            )
            params.append(limit)

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)
                return [dict(row) for row in rows]

        except Exception as exc:
            logger.error("查询 decision_log 失败", error=str(exc))
            return []


__all__ = [
    "DecisionRecord",
    "DecisionLogger",
    "_DB_CONFIG",
]

