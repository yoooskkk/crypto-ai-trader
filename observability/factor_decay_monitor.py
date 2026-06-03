"""
因子衰减监控器 — 定时调度层
所属层级: 可观测性层 (Observability)
下层引擎: validation/factor_decay.py (核心分析逻辑)
数据写入: InfluxDB (存储 IC 时序)
输出去向: Grafana 仪表板 / AlertManager 告警

职责:
  1. 定时从 InfluxDB 读取因子 IC 时序数据
  2. 调用 FactorDecayMonitor.analyze() 分析衰减
  3. 检测到衰减时: 写 InfluxDB + 发告警
  4. 暴露 /metrics 端点供 Prometheus 抓取

用法:
    from observability.factor_decay_monitor import FactorDecayMonitorScheduler

    scheduler = FactorDecayMonitorScheduler()
    result = scheduler.run_once(factor_name="momentum_1", ic_series=[0.05, 0.04, ...])

环境变量:
    INFLUXDB_URL      InfluxDB 地址 (默认 http://localhost:8086)
    INFLUXDB_TOKEN    InfluxDB token
    INFLUXDB_ORG      InfluxDB 组织名称
    INFLUXDB_BUCKET   InfluxDB bucket (默认 factor_decay)
    DECAY_CHECK_INTERVAL  检查间隔分钟 (默认 60)
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from validation.factor_decay import (
    FactorDecayConfig,
    FactorDecayMonitor as CoreMonitor,
    FactorDecayReport as CoreReport,
)

logger = structlog.get_logger(__name__)

# ─── 配置 ─────────────────────────────────────────────────

_INFLUXDB_URL    = os.getenv("INFLUXDB_URL", "http://localhost:8086")
_INFLUXDB_TOKEN  = os.getenv("INFLUXDB_TOKEN", "")
_INFLUXDB_ORG    = os.getenv("INFLUXDB_ORG", "crypto_trader")
_INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "factor_decay")
_CHECK_INTERVAL  = int(os.getenv("DECAY_CHECK_INTERVAL", "60"))

_DEFAULT_FACTORS = [
    "momentum_1", "momentum_2", "trend_1", "volume_1",
    "volatility_1", "crypto_funding", "crypto_oi", "crypto_cvd",
]


# ─── 结果模型 ─────────────────────────────────────────────

@dataclass
class FactorDecayResult:
    """一次因子衰减检查的结果。"""
    factor_name: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat() + "Z")
    is_decaying: bool = False
    ic_mean: float = 0.0
    ic_recent_mean: float = 0.0
    ic_slope: float = 0.0
    half_life: int = 0
    ic_values_count: int = 0
    detail: str = ""

    @staticmethod
    def from_core_report(report: CoreReport) -> FactorDecayResult:
        return FactorDecayResult(
            factor_name=report.factor_name,
            is_decaying=report.is_decaying,
            ic_mean=report.ic_mean,
            ic_recent_mean=report.ic_mean,
            ic_slope=report.ic_trend_slope,
            half_life=report.ic_half_life,
            ic_values_count=len(report.ic_values),
            detail=report.alert_message,
        )

    def to_influxdb_point(self) -> dict[str, Any]:
        return {
            "measurement": "factor_decay",
            "tags": {"factor": self.factor_name},
                        "fields": {
                "is_decaying": 1 if self.is_decaying else 0,
                "ic_mean": round(self.ic_mean, 6),
                "ic_slope": round(self.ic_slope, 6) if self.ic_slope else 0.0,
                "half_life": self.half_life,
                "ic_count": self.ic_values_count,
            },
            "time": self.timestamp,
        }

    def to_prometheus_metrics(self) -> str:
        lines = [
            '# HELP factor_decay_is_decaying 因子是否处于衰减状态 (1=是, 0=否)',
            '# TYPE factor_decay_is_decaying gauge',
            f'factor_decay_is_decaying{{factor="{self.factor_name}"}} {1 if self.is_decaying else 0}',
            "",
            '# HELP factor_decay_ic_mean 因子 IC 均值',
            '# TYPE factor_decay_ic_mean gauge',
            f'factor_decay_ic_mean{{factor="{self.factor_name}"}} {self.ic_mean:.6f}',
            "",
            '# HELP factor_decay_ic_slope 因子 IC 斜率',
            '# TYPE factor_decay_ic_slope gauge',
            f'factor_decay_ic_slope{{factor="{self.factor_name}"}} {self.ic_slope:.6f}',
            "",
            '# HELP factor_decay_half_life 因子半衰期（天数）',
            '# TYPE factor_decay_half_life gauge',
            f'factor_decay_half_life{{factor="{self.factor_name}"}} {self.half_life}',
        ]
        return "\n".join(lines)


# ─── 调度器 ─────────────────────────────────────────────

class FactorDecayMonitorScheduler:
    """
    因子衰减监控调度器。

    它可以：
    - run_once(): 对单个因子执行一次衰减分析
    - run_all(): 对所有已配置因子执行一次
    - write_to_influxdb(): 将结果写入 InfluxDB
    - start(): 进入循环调度模式（供后台进程使用）
    """

    def __init__(
        self,
        config: Optional[FactorDecayConfig] = None,
        factors: Optional[list[str]] = None,
        influxdb_client: Any = None,
    ) -> None:
        self._config = config or FactorDecayConfig()
        self._factors = factors or list(_DEFAULT_FACTORS)
        self._core = CoreMonitor(config=self._config)
        self._influxdb = influxdb_client
        self._influxdb_available = False

        if self._influxdb is not None:
            self._influxdb_available = True
        else:
            try:
                __import__("influxdb_client")
                self._influxdb_available = True
            except ImportError:
                logger.warning(
                    "influxdb_client 未安装，InfluxDB 写入不可用"
                )

        self._running = False
        self._last_results: dict[str, FactorDecayResult] = {}

    def run_once(self, factor_name: str, ic_series: list[float]) -> FactorDecayResult:
        report = self._core.analyze(factor_name, ic_series)
        result = FactorDecayResult.from_core_report(report)
        self._last_results[factor_name] = result

        if result.is_decaying:
            logger.warning(
                "因子衰减检测",
                factor=factor_name,
                ic_mean=result.ic_mean,
                ic_slope=result.ic_slope,
                half_life=result.half_life,
                detail=result.detail,
            )
        else:
            logger.debug(
                "因子状态正常",
                factor=factor_name,
                ic_mean=result.ic_mean,
                ic_slope=result.ic_slope,
            )

        return result

    def run_all(self, data_provider: Any = None) -> list[FactorDecayResult]:
        results: list[FactorDecayResult] = []

        for factor in self._factors:
            ic_series: list[float] = []

            if data_provider is not None:
                ic_series = data_provider.get_ic_series(factor)
            elif self._influxdb_available:
                ic_series = self._read_ic_from_influxdb(factor)
            else:
                logger.debug("跳过因子检查（无数据源）", factor=factor)
                continue

            if len(ic_series) < 2:
                logger.debug("IC 数据不足，跳过", factor=factor, count=len(ic_series))
                continue

            result = self.run_once(factor, ic_series)
            results.append(result)

        return results

    def write_to_influxdb(self, results: list[FactorDecayResult]) -> int:
        if not self._influxdb_available:
            logger.warning("InfluxDB 不可用，跳过写入")
            return 0

        try:
            from influxdb_client import InfluxDBClient, Point
            from influxdb_client.client.write_api import SYNCHRONOUS

            if self._influxdb is None:
                self._influxdb = InfluxDBClient(
                    url=_INFLUXDB_URL,
                    token=_INFLUXDB_TOKEN,
                    org=_INFLUXDB_ORG,
                )

            client: InfluxDBClient = self._influxdb
            write_api = client.write_api(write_options=SYNCHRONOUS)

            written = 0
            for r in results:
                point = (
                    Point("factor_decay")
                    .tag("factor", r.factor_name)
                    .field("is_decaying", 1 if r.is_decaying else 0)
                                        .field("ic_mean", round(r.ic_mean, 6))
                    .field("ic_slope", round(r.ic_slope, 6) if r.ic_slope else 0.0)
                    .field("half_life", r.half_life)
                    .field("ic_count", r.ic_values_count)
                    .time(r.timestamp)
                )
                write_api.write(bucket=_INFLUXDB_BUCKET, record=point)
                written += 1

            logger.info("因子衰减数据已写入 InfluxDB", count=written)
            return written

        except Exception as exc:
            logger.error("InfluxDB 写入失败", error=str(exc))
            return 0

    def _read_ic_from_influxdb(self, factor_name: str) -> list[float]:
        if not self._influxdb_available:
            return []

        try:
            from influxdb_client import InfluxDBClient

            if self._influxdb is None:
                self._influxdb = InfluxDBClient(
                    url=_INFLUXDB_URL,
                    token=_INFLUXDB_TOKEN,
                    org=_INFLUXDB_ORG,
                )

            client: InfluxDBClient = self._influxdb
            query_api = client.query_api()

            flux = f'''
            from(bucket: "{_INFLUXDB_BUCKET}")
              |> range(start: -90d)
              |> filter(fn: (r) => r["_measurement"] == "factor_ic")
              |> filter(fn: (r) => r["factor"] == "{factor_name}")
              |> filter(fn: (r) => r["_field"] == "ic")
              |> sort(columns: ["_time"])
              |> yield(name: "mean")
            '''

            tables = query_api.query(flux)
            values = []
            for table in tables:
                for record in table.records:
                    v = record.get_value()
                    if v is not None:
                        try:
                            values.append(float(v))
                        except (ValueError, TypeError):
                            continue
            return values

        except Exception as exc:
            logger.error("从 InfluxDB 读取 IC 失败", error=str(exc))
            return []

    def get_prometheus_metrics(self) -> str:
        if not self._last_results:
            return "# No factor decay data available\n"

        lines = []
        for result in self._last_results.values():
            lines.append(result.to_prometheus_metrics())
        return "\n".join(lines)

    def start(self, data_provider: Any = None) -> None:
        self._running = True
        logger.info(
            "因子衰减监控调度器启动",
            interval_minutes=_CHECK_INTERVAL,
            factors=self._factors,
        )

        while self._running:
            try:
                results = self.run_all(data_provider=data_provider)

                if results:
                    written = self.write_to_influxdb(results)
                    logger.info(
                        "因子衰减调度完成",
                        checked=len(results),
                        written=written,
                    )

                    decaying = [r for r in results if r.is_decaying]
                    for r in decaying:
                        logger.critical(
                            "因子衰减告警",
                            factor=r.factor_name,
                            ic_mean=r.ic_mean,
                            ic_slope=r.ic_slope,
                            half_life=r.half_life,
                        )

                time.sleep(_CHECK_INTERVAL * 60)

            except KeyboardInterrupt:
                logger.info("因子衰减监控调度器停止（用户中断）")
                self._running = False
                break
            except Exception as exc:
                logger.error("因子衰减监控调度异常", error=str(exc))
                time.sleep(60)

    def stop(self) -> None:
        self._running = False
        logger.info("因子衰减监控调度器已停止")


# ─── 快捷函数 ─────────────────────────────────────────────

def run_check(
    factor_name: str,
    ic_series: list[float],
    config: Optional[FactorDecayConfig] = None,
    influxdb_client: Any = None,
) -> FactorDecayResult:
    scheduler = FactorDecayMonitorScheduler(
        config=config,
        influxdb_client=influxdb_client,
    )
    result = scheduler.run_once(factor_name, ic_series)
    scheduler.write_to_influxdb([result])
    return result


def run_all_checks(
    factor_ic_map: dict[str, list[float]],
    config: Optional[FactorDecayConfig] = None,
    influxdb_client: Any = None,
) -> list[FactorDecayResult]:
    scheduler = FactorDecayMonitorScheduler(
        config=config,
        factors=list(factor_ic_map.keys()),
        influxdb_client=influxdb_client,
    )
    results = []
    for factor_name, ic_series in factor_ic_map.items():
        result = scheduler.run_once(factor_name, ic_series)
        results.append(result)
    scheduler.write_to_influxdb(results)
    return results


# ─── CLI 入口 ─────────────────────────────────────────────

def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="因子衰减监控调度器")
    parser.add_argument("--once", action="store_true", help="执行一次检查后退出")
    parser.add_argument("--factor", type=str, default=None, help="指定因子名称")
    parser.add_argument("--interval", type=int, default=_CHECK_INTERVAL, help=f"检查间隔分钟（默认 {_CHECK_INTERVAL}）")
    parser.add_argument("--metrics", action="store_true", help="输出 Prometheus metrics")
    args = parser.parse_args()

    scheduler = FactorDecayMonitorScheduler()

    if args.metrics:
        print(scheduler.get_prometheus_metrics())
        return 0

    if args.factor:
        print(f"请输入 {args.factor} 的 IC 序列（逗号分隔，例如 0.05,0.04,0.03）：")
        try:
            line = input().strip()
            ic_series = [float(x.strip()) for x in line.split(",") if x.strip()]
            result = scheduler.run_once(args.factor, ic_series)
            print(f"因子: {result.factor_name}")
            print(f"衰减: {'是 ⚠️' if result.is_decaying else '否 ✅'}")
            print(f"IC 均值: {result.ic_mean:.4f}")
            print(f"IC 斜率: {result.ic_slope:.6f}")
            print(f"半衰期: {result.half_life}d")
            if result.detail:
                print(f"详情: {result.detail}")
            scheduler.write_to_influxdb([result])
        except Exception as exc:
            logger.error("输入解析失败", error=str(exc))
            return 1
    elif args.once:
        results = scheduler.run_all()
        print(f"检查完成: {len(results)} 个因子")
        for r in results:
            status = "⚠️" if r.is_decaying else "✅"
            print(f"  {status} {r.factor_name}: IC={r.ic_mean:.4f}, slope={r.ic_slope:.6f}")
    else:
        scheduler.start()

    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "FactorDecayResult",
    "FactorDecayMonitorScheduler",
    "run_check",
    "run_all_checks",
    "main",
]
