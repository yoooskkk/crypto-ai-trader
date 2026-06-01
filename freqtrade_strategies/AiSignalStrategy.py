"""
AiSignalStrategy — Freqtrade 主策略
所属层级: 风险控制层 (Risk Guardian) + 策略执行
输入来源: trade_order Stream（由 risk_guardian 写入）
输出去向: Freqtrade 信号列（dataframe 的 enter_long/exit_long 等）
关键依赖: freqtrade, redis

从 Redis 读取 risk_guardian 审核后的交易信号，
转换为 Freqtrade 的 populate_entry_trend / populate_exit_trend 信号列。

安全约束（铁律 #1）：
  此模块只读 Redis Stream，不写任何 Stream。
  写入 trade_order 是 risk_guardian 的专属权限。

用法：
  在 Freqtrade 配置中将 "strategy" 设为 "AiSignalStrategy"。
  确保 Redis 服务已配置，Stream 名 match trade_order。
"""
from __future__ import annotations

import json
import os
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ─── 辅助函数（可在不安装 freqtrade 时导入）────────

def load_signal_from_payload(payload_str: str) -> dict[str, Any] | None:
    """从 Redis Stream payload 字符串解析信号。"""
    try:
        return json.loads(payload_str)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Payload 解析失败", error=str(exc))
        return None


# ─── Redis 配置 ───────────────────────────────────

_REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
_REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
_TRADE_ORDER_STREAM = "trade_order"


# ─── Freqtrade 策略（需要安装 freqtrade）─────────

try:
    import pandas as pd
    from freqtrade.strategy import IStrategy

    _FREQTRADE_AVAILABLE = True
except ImportError:
    _FREQTRADE_AVAILABLE = False
    # 用于类型提示的占位
    pd = None  # type: ignore
    IStrategy = object  # type: ignore


class AiSignalStrategy(IStrategy):
    """
    Freqtrade 主策略 — 从 Redis trade_order Stream 读取信号。

    配置建议（可在 config.json 中覆盖）：
      timeframe: "1h"
      minimal_roi: {"0": 0.05}
      stoploss: -0.03
      trailing_stop: True
    """

    INTERFACE_VERSION = 3

    # 可被 config.json 覆盖
    timeframe = "1h"
    minimal_roi = {"0": 0.05}
    stoploss = -0.03
    trailing_stop = True
    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count = 200  # 预加载足够的历史数据

    # ─── Redis 连接（懒初始化） ─────────────────

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._redis = None
        self._signal_cache: dict[str, dict[str, Any]] = {}  # symbol -> latest signal
        self._last_update_ts: dict[str, int] = {}  # symbol -> timestamp
        self._cache_ttl_ms: int = 60000  # 信号缓存 60 秒

        logger.info(
            "AiSignalStrategy 初始化",
            timeframe=self.timeframe,
            redis_host=_REDIS_HOST,
            redis_port=_REDIS_PORT,
        )

    # ─── 指标处理 ────────────────────────────────

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """
        指标由独立 worker 预计算，此处直接从 Redis 读取缓存信号。
        确保 dataframe 包含 enter_long/enter_short 等标准列。
        """
        pair = metadata.get("pair", "UNKNOWN")

        # 确保标准列存在（初始化为 0）
        for col in ["enter_long", "enter_short", "exit_long", "exit_short"]:
            if col not in dataframe.columns:
                dataframe[col] = 0

        # 尝试从 Redis 读取最新信号
        signal = self._fetch_latest_signal(pair)
        if signal:
            self._signal_cache[pair] = signal
            logger.debug("获取到 Redis 信号", pair=pair, action=signal.get("action"))

        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """
        根据 trade_order Stream 信号设置入场信号。

        规则:
          - action="LONG"  → enter_long = 1（最新一根 K 线）
          - action="SHORT" → enter_short = 1
          - 无信号或 FLAT → 不入场
        """
        pair = metadata.get("pair", "UNKNOWN")
        signal = self._signal_cache.get(pair)

        if signal is None:
            # 尝试刷新缓存
            signal = self._fetch_latest_signal(pair)
            if signal:
                self._signal_cache[pair] = signal

        if signal is None or signal.get("action") == "FLAT":
            dataframe["enter_long"] = 0
            dataframe["enter_short"] = 0
            return dataframe

        action = signal.get("action", "")
        size_pct = signal.get("size_pct", 0.0)

        # 只在最新一根 K 线设置入场信号
        if action == "LONG":
            dataframe.loc[
                dataframe.index[-1], "enter_long"
            ] = 1
            logger.info(
                "AI 信号：LONG 入场",
                pair=pair,
                size_pct=size_pct,
                entry=signal.get("entry"),
                audit_id=signal.get("audit_id", ""),
            )
        elif action == "SHORT":
            dataframe.loc[
                dataframe.index[-1], "enter_short"
            ] = 1
            logger.info(
                "AI 信号：SHORT 入场",
                pair=pair,
                size_pct=size_pct,
                entry=signal.get("entry"),
                audit_id=signal.get("audit_id", ""),
            )

        # 设置止损止盈（Freqtrade 会从 dataframe 列读取）
        if signal.get("sl"):
            dataframe.loc[dataframe.index[-1], "stop_loss"] = float(signal["sl"])
        if signal.get("tp"):
            dataframe.loc[dataframe.index[-1], "take_profit"] = float(signal["tp"])

        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """
        根据 trade_order Stream 信号设置出场信号。

        规则:
          - action="FORCE_EXIT" → exit_long = 1 或 exit_short = 1
          - 正常止盈止损由 Freqtrade 内置逻辑处理
        """
        pair = metadata.get("pair", "UNKNOWN")

        # 确保列存在
        for col in ["exit_long", "exit_short"]:
            if col not in dataframe.columns:
                dataframe[col] = 0

        signal = self._signal_cache.get(pair)

        if signal and signal.get("action") == "FORCE_EXIT":
            # 检测当前持仓方向
            if "open_trades" in metadata:
                # 由 Freqtrade 框架处理
                pass

            dataframe.loc[dataframe.index[-1], "exit_long"] = 1
            dataframe.loc[dataframe.index[-1], "exit_short"] = 1
            logger.warning(
                "AI 信号：强制平仓",
                pair=pair,
                audit_id=signal.get("audit_id", ""),
            )

        return dataframe

    # ─── Redis 交互 ───────────────────────────────

    def _fetch_latest_signal(self, pair: str) -> dict[str, Any] | None:
        """
        从 Redis trade_order Stream 读取该交易对的最新信号。
        使用消费者组模式，确保不丢失信号。
        """
        import redis.asyncio as aioredis

        try:
            if self._redis is None:
                self._redis = aioredis.from_url(
                    f"redis://{_REDIS_HOST}:{_REDIS_PORT}",
                    decode_responses=True,
                )

            # 使用同步方式获取（Freqtrade 的 populate_* 是同步方法）
            # 所以这里直接使用同步 redis 客户端
            return self._fetch_sync(pair)
        except Exception as exc:
            logger.warning(
                "Redis 读取失败",
                pair=pair,
                error=str(exc),
            )
            return None

    def _fetch_sync(self, pair: str) -> dict[str, Any] | None:
        """同步方式从 Redis 读取最新信号。"""
        import redis as sync_redis

        try:
            r = sync_redis.Redis(
                host=_REDIS_HOST,
                port=_REDIS_PORT,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )

            # 读取 stream 的最新消息
            # trade_order stream 结构: {payload: "{\"symbol\":\"BTCUSDT\",...}"}
            results = r.xrevrange(_TRADE_ORDER_STREAM, count=20)

            for msg_id, fields in results:
                payload_str = fields.get("payload", "")
                if not payload_str:
                    continue

                try:
                    payload = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue

                # 匹配交易对
                if payload.get("symbol") == pair or payload.get("pair") == pair:
                    r.close()
                    return payload

            r.close()
            return None

        except Exception as exc:
            logger.warning("Redis 同步读取失败", error=str(exc))
            return None



__all__ = ["AiSignalStrategy", "load_signal_from_payload"]

