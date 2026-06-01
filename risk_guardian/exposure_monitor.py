"""
模块名称: exposure_monitor.py
所属层级: 风险控制层 (Risk Guardian)
输入来源: Freqtrade REST API（读取持仓状态）
输出去向: 内部告警（不产生 Stream 消息，仅日志 + 返回值）
关键依赖: config/risk.yml

实时持仓风险监控器。
通过 Freqtrade REST API 获取当前持仓和账户信息，
计算已开仓 USD / 总资产比例，超出 MAX_EXPOSURE_PCT 时告警。

重要限制：
  - 只告警，不平仓（平仓权限仅归 circuit_breaker）
  - Freqtrade API 不可用时不抛异常，仅记录 warning
  - 所有 Freqtrade API 调用由调用方提供（可 mock）
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

import structlog
import yaml

logger = structlog.get_logger(__name__)


# ─── 配置路径 ────────────────────────────────────

_CONFIG_DIR = Path(__file__).parent.parent / "config"
RISK_CONFIG_PATH = _CONFIG_DIR / "risk.yml"


# ─── 默认配置 ─────────────────────────────────────

_DEFAULT_MAX_EXPOSURE_PCT = 80.0  # 80%
_DEFAULT_CHECK_INTERVAL_SEC = 60  # 每分钟检查一次


# ─── 辅助函数 ────────────────────────────────

def _load_exposure_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """从 risk.yml 加载持仓监控参数。"""
    cfg_path = Path(config_path) if config_path else RISK_CONFIG_PATH

    defaults = {
        "max_total_pct": _DEFAULT_MAX_EXPOSURE_PCT,
        "max_single_position_pct": 20.0,
        "max_correlated_pairs": 3,
    }

    if not cfg_path.exists():
        logger.warning("risk.yml 未找到，使用默认持仓配置", path=str(cfg_path))
        return defaults

    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        exp_cfg = cfg.get("exposure", {})
        if not exp_cfg:
            return defaults

        return {
            "max_total_pct": float(exp_cfg.get("max_total_pct", defaults["max_total_pct"])),
            "max_single_position_pct": float(
                exp_cfg.get("max_single_position_pct", defaults["max_single_position_pct"])
            ),
            "max_correlated_pairs": int(
                exp_cfg.get("max_correlated_pairs", defaults["max_correlated_pairs"])
            ),
        }
    except Exception as exc:
        logger.error("加载持仓配置失败，使用默认值", error=str(exc))
        return defaults


# ─── 持仓信息模型 ────────────────────────────────

@dataclass
class PositionInfo:
    """单一持仓信息。"""
    symbol: str
    side: str                   # "long" 或 "short"
    size_usd: float             # 持仓 USD 价值
    unrealized_pnl_usd: float   # 未实现盈亏
    entry_price: float
    current_price: float


@dataclass
class ExposureSnapshot:
    """一次持仓检查的快照。"""
    total_equity_usd: float     # 总资产 USD
    total_exposure_usd: float   # 总持仓 USD
    exposure_pct: float         # 持仓比例 %
    positions: list[PositionInfo]
    max_exposure_pct: float     # 配置中的上限 %
    is_exceeded: bool           # 是否超限
    exceed_by_pct: float        # 超限幅度 %
    correlated_pair_count: int  # 关联交易对数
    max_correlated_pairs: int   # 配置中的关联对上限


# ─── 持仓监控器 ─────────────────────────────────

class ExposureMonitor:
    """
    实时持仓风险监控器。

    定期检查当前持仓占总资产比例，超限时告警。
    不平仓（平仓权限仅归 circuit_breaker）。

    用法:
        monitor = ExposureMonitor()

        # 方式 1：手动检查
        snapshot = await monitor.check(
            fetch_positions=my_async_fetch_fn,
            fetch_equity=my_async_equity_fn,
        )

        # 方式 2：启动后台监控循环
        stop_event = asyncio.Event()
        asyncio.create_task(monitor.start_monitoring(
            fetch_positions=my_async_fetch_fn,
            fetch_equity=my_async_equity_fn,
            stop_event=stop_event,
        ))
    """

    def __init__(
        self,
        max_exposure_pct: float | None = None,
        max_correlated_pairs: int | None = None,
        check_interval_sec: float = _DEFAULT_CHECK_INTERVAL_SEC,
    ):
        """
        初始化持仓监控器。

        参数:
            max_exposure_pct: 总持仓上限百分比，默认从 risk.yml 读取
            max_correlated_pairs: 最大关联交易对数，默认从 risk.yml 读取
            check_interval_sec: 自动检查间隔秒数
        """
        config = _load_exposure_config()
        self._max_exposure_pct = (
            float(max_exposure_pct) if max_exposure_pct is not None
            else config["max_total_pct"]
        )
        self._max_correlated = (
            int(max_correlated_pairs) if max_correlated_pairs is not None
            else config["max_correlated_pairs"]
        )
        self._check_interval = check_interval_sec

        # 上次告警时间（防重复告警 flooding）
        self._last_warn_time: float = 0.0
        self._min_warn_interval: float = 300.0  # 至少 5 分钟重复告警

        logger.info(
            "ExposureMonitor 初始化",
            max_exposure_pct=self._max_exposure_pct,
            max_correlated_pairs=self._max_correlated,
            check_interval_sec=self._check_interval,
        )

    async def check(
        self,
        fetch_positions: Callable[[], Awaitable[list[dict]]] | None = None,
        fetch_equity: Callable[[], Awaitable[float]] | None = None,
    ) -> ExposureSnapshot:
        """
        执行一次持仓检查。

        参数:
            fetch_positions: 异步函数，返回持仓列表 [{symbol, side, size, ...}]
                            如果为 None，跳过持仓检查（总持仓为 0）
            fetch_equity: 异步函数，返回总资产 USD
                          如果为 None，跳过权益检查

        返回:
            ExposureSnapshot
        """
        positions: list[PositionInfo] = []
        total_exposure = 0.0
        total_equity = 0.0

        # ─── 获取持仓 ──────────────────────────
        if fetch_positions is not None:
            try:
                raw_positions = await fetch_positions()
                for pos in raw_positions:
                    p = self._parse_position(pos)
                    if p:
                        positions.append(p)
                        total_exposure += p.size_usd
            except asyncio.TimeoutError:
                logger.warning("Freqtrade API 获取持仓超时")
            except Exception as exc:
                logger.warning("Freqtrade API 获取持仓失败", error=str(exc))
        else:
            logger.debug("未提供 fetch_positions，跳过持仓获取")

        # ─── 获取总资产 ──────────────────────────
        if fetch_equity is not None:
            try:
                total_equity = await fetch_equity()
            except asyncio.TimeoutError:
                logger.warning("Freqtrade API 获取总资产超时")
            except Exception as exc:
                logger.warning("Freqtrade API 获取总资产失败", error=str(exc))

        # ─── 计算比例 ──────────────────────────
        if total_equity > 0:
            exposure_pct = (total_exposure / total_equity) * 100.0
        else:
            exposure_pct = 0.0

        is_exceeded = exposure_pct > self._max_exposure_pct
        exceed_by = max(0.0, exposure_pct - self._max_exposure_pct)

        # ─── 计算关联交易对数（同类型币种分组）─
        correlated_count = self._count_correlated(positions)

        snapshot = ExposureSnapshot(
            total_equity_usd=round(total_equity, 2),
            total_exposure_usd=round(total_exposure, 2),
            exposure_pct=round(exposure_pct, 2),
            positions=positions,
            max_exposure_pct=self._max_exposure_pct,
            is_exceeded=is_exceeded,
            exceed_by_pct=round(exceed_by, 2),
            correlated_pair_count=correlated_count,
            max_correlated_pairs=self._max_correlated,
        )

        # ─── 告警 ──────────────────────────────
        if is_exceeded:
            self._warn_if_needed(snapshot)

        logger.debug(
            "持仓检查完成",
            exposure_pct=exposure_pct,
            total_exposure=round(total_exposure, 2),
            total_equity=round(total_equity, 2),
            positions=len(positions),
            is_exceeded=is_exceeded,
        )

        return snapshot

    async def start_monitoring(
        self,
        fetch_positions: Callable[[], Awaitable[list[dict]]],
        fetch_equity: Callable[[], Awaitable[float]],
        stop_event: asyncio.Event,
    ) -> None:
        """
        启动后台监控循环。

        参数:
            fetch_positions: 异步持仓获取函数
            fetch_equity: 异步总资产获取函数
            stop_event: 设置此事件以停止监控
        """
        logger.info("持仓监控循环启动", interval_sec=self._check_interval)

        while not stop_event.is_set():
            try:
                await self.check(fetch_positions, fetch_equity)
            except Exception as exc:
                logger.error("持仓监控异常", error=str(exc))

            try:
                await asyncio.wait_for(
                    asyncio.get_event_loop().wait(
                        stop_event.wait() for _ in [None]
                    ),
                    timeout=self._check_interval,
                )
            except (asyncio.TimeoutError, StopIteration):
                pass

        logger.info("持仓监控循环已停止")

    # ─── 内部辅助 ────────────────────────────

    @staticmethod
    def _parse_position(raw: dict) -> PositionInfo | None:
        """将 Freqtrade API 返回的持仓字典解析为 PositionInfo。"""
        try:
            symbol = raw.get("symbol") or raw.get("pair", "")
            if not symbol:
                return None

            return PositionInfo(
                symbol=str(symbol),
                side=str(raw.get("side", raw.get("direction", "long"))),
                size_usd=abs(float(raw.get("size_usd", raw.get("stake_amount", 0)))),
                unrealized_pnl_usd=float(raw.get("unrealized_pnl", 0)),
                entry_price=float(raw.get("entry_price", raw.get("open_rate", 0))),
                current_price=float(raw.get("current_price", raw.get("current_rate", 0))),
            )
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("解析持仓失败", raw=raw, error=str(exc))
            return None

    def _warn_if_needed(self, snapshot: ExposureSnapshot) -> None:
        """防 flooding 告警。"""
        import time
        now = time.time()
        if now - self._last_warn_time < self._min_warn_interval:
            return

        self._last_warn_time = now

        logger.warning(
            "⚠️ 持仓超限告警",
            exposure_pct=snapshot.exposure_pct,
            max_exposure_pct=snapshot.max_exposure_pct,
            exceed_by_pct=snapshot.exceed_by_pct,
            total_exposure_usd=snapshot.total_exposure_usd,
            total_equity_usd=snapshot.total_equity_usd,
            position_count=len(snapshot.positions),
            correlated_pairs=snapshot.correlated_pair_count,
        )

        if snapshot.correlated_pair_count > snapshot.max_correlated_pairs:
            logger.warning(
                "关联交易对超限",
                count=snapshot.correlated_pair_count,
                max=snapshot.max_correlated_pairs,
            )

    @staticmethod
    def _count_correlated(positions: list[PositionInfo]) -> int:
        """
        统计关联交易对数量。
        按基础币种分组（如 BTCUSDT + BTCETH = 2 个 BTC 相关）。
        """
        base_currencies: dict[str, int] = {}
        for pos in positions:
            sym = pos.symbol
            # 提取基础币种（取前 3-4 字符）
            # 常见的币对格式：BTCUSDT, ETHUSDT, SOLUSDT
            base = sym[:-4] if sym.endswith("USDT") else sym
            base_currencies[base] = base_currencies.get(base, 0) + 1

        # 计数超过 1 的为基础币种关联
        return sum(1 for count in base_currencies.values() if count > 1)


__all__ = [
    "ExposureMonitor",
    "PositionInfo",
    "ExposureSnapshot",
    "_load_exposure_config",
]

