"""
健康检查脚本 — 一键检测所有服务状态
用法:
    python -m scripts.health_check              # 人类可读输出
    python -m scripts.health_check --json       # JSON 输出（供监控系统）
    python -m scripts.health_check --service redis  # 只检查 Redis

返回值: 0（全部正常） 或 1（存在异常）
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ─── 服务配置 ─────────────────────────────────────────────────

SERVICE_CONFIGS: dict[str, dict[str, Any]] = {
    "redis": {
        "host": os.getenv("REDIS_HOST", "localhost"),
        "port": int(os.getenv("REDIS_PORT", "6379")),
        "timeout": 3,
    },
    "timescaledb": {
        "host": os.getenv("TIMESCALEDB_HOST", "localhost"),
        "port": int(os.getenv("TIMESCALEDB_PORT", "5432")),
        "timeout": 3,
    },
    "freqtrade_api": {
        "url": os.getenv("FREQTRADE_API_URL", "http://freqtrade:8080"),
        "endpoint": "/api/v1/ping",
        "timeout": 5,
    },
    "influxdb": {
        "host": os.getenv("INFLUXDB_HOST", "localhost"),
        "port": int(os.getenv("INFLUXDB_PORT", "8086")),
        "timeout": 3,
    },
}


# ─── 结果模型 ─────────────────────────────────────────────────

@dataclass
class ServiceResult:
    """单个服务检查结果。"""
    name: str
    status: str          # "ok" / "error" / "skip"
    latency_ms: float = 0.0
    detail: str = ""


@dataclass
class HealthReport:
    """完整健康检查报告。"""
    status: str          # "healthy" / "degraded" / "unhealthy"
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    services: list[ServiceResult] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        result = {"ok": 0, "error": 0, "skip": 0, "total": 0}
        for srv in self.services:
            result[srv.status] = result.get(srv.status, 0) + 1
            result["total"] += 1
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "timestamp": self.timestamp,
            "summary": self.summary,
            "services": [asdict(s) for s in self.services],
        }


# ─── 检查函数 ─────────────────────────────────────────────────

def check_tcp(service: str, host: str, port: int, timeout: int) -> ServiceResult:
    """TCP 端口连通性检查。"""
    start = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        latency = (time.time() - start) * 1000
        return ServiceResult(
            name=service,
            status="ok",
            latency_ms=round(latency, 1),
        )
    except socket.timeout:
        return ServiceResult(
            name=service,
            status="error",
            detail=f"连接超时（{timeout}s）",
        )
    except ConnectionRefusedError:
        return ServiceResult(
            name=service,
            status="error",
            detail=f"连接被拒绝（{host}:{port}）",
        )
    except Exception as exc:
        return ServiceResult(
            name=service,
            status="error",
            detail=str(exc),
        )


def check_freqtrade_api(url: str, endpoint: str, timeout: int) -> ServiceResult:
    """Freqtrade REST API 健康检查。"""
    try:
        __import__("requests")
    except ImportError:
        return ServiceResult(
            name="freqtrade_api",
            status="skip",
            detail="requests 模块未安装",
        )

    import requests as req

    target = f"{url.rstrip('/')}{endpoint}"
    start = time.time()
    try:
        resp = req.get(target, timeout=timeout)
        latency = (time.time() - start) * 1000
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "pong":
                return ServiceResult(
                    name="freqtrade_api",
                    status="ok",
                    latency_ms=round(latency, 1),
                )
        return ServiceResult(
            name="freqtrade_api",
            status="error",
            detail=f"HTTP {resp.status_code}: {resp.text[:100]}",
            latency_ms=round(latency, 1),
        )
    except Exception as exc:
        return ServiceResult(
            name="freqtrade_api",
            status="error",
            detail=str(exc),
        )


def check_redis(
    host: str = "localhost",
    port: int = 6379,
    timeout: int = 3,
) -> ServiceResult:
    """Redis PING 检查（使用 redis-py，回退到 TCP 检查）。"""
    try:
        __import__("redis")
        import redis as r

        start = time.time()
        client = r.Redis(host=host, port=port, socket_timeout=timeout)
        pong = client.ping()
        latency = (time.time() - start) * 1000
        if pong:
            return ServiceResult(
                name="redis",
                status="ok",
                latency_ms=round(latency, 1),
            )
        return ServiceResult(name="redis", status="error", detail="PING 返回 False")
    except ImportError:
        # 降级到 TCP 检查
        return check_tcp("redis", host, port, timeout)
    except Exception as exc:
        return ServiceResult(name="redis", status="error", detail=str(exc))


def check_timescaledb(
    host: str = "localhost",
    port: int = 5432,
    timeout: int = 3,
) -> ServiceResult:
    """TimescaleDB 检查（使用 asyncpg 连接测试，降级到 TCP）。"""
    try:
        __import__("asyncpg")
    except ImportError:
        return check_tcp("timescaledb", host, port, timeout)

    import asyncio

    async def _check() -> ServiceResult:
        import asyncpg
        start = time.time()
        try:
            conn = await asyncpg.connect(
                host=host,
                port=port,
                user=os.getenv("TIMESCALEDB_USER", "trader"),
                password=os.getenv("TIMESCALEDB_PASSWORD", "trader"),
                database=os.getenv("TIMESCALEDB_DB", "crypto_trader"),
                timeout=timeout,
            )
            version = await conn.fetchval("SELECT version()")
            await conn.close()
            latency = (time.time() - start) * 1000
            return ServiceResult(
                name="timescaledb",
                status="ok",
                latency_ms=round(latency, 1),
                detail=str(version)[:50],
            )
        except Exception as exc:
            return ServiceResult(
                name="timescaledb",
                status="error",
                detail=str(exc),
            )

    try:
        return asyncio.run(_check())
    except Exception as exc:
        return ServiceResult(name="timescaledb", status="error", detail=str(exc))


# ─── 主逻辑 ─────────────────────────────────────────────────

def run_health_check(service_filter: str | None = None) -> HealthReport:
    """
    执行所有服务健康检查。

    参数:
        service_filter: 可选，只检查指定服务（如 "redis"）

    返回:
        HealthReport
    """
    report = HealthReport(status="healthy")
    checks: list[tuple[str, dict[str, Any]]] = []

    for name, cfg in SERVICE_CONFIGS.items():
        if service_filter and name != service_filter:
            continue
        checks.append((name, cfg))

    for name, cfg in checks:
        if name == "redis":
            result = check_redis(**cfg)
        elif name == "timescaledb":
            result = check_timescaledb(**cfg)
        elif name == "freqtrade_api":
            result = check_freqtrade_api(**cfg)
        elif name == "influxdb":
            result = check_tcp(name, **cfg)
        else:
            result = ServiceResult(name=name, status="skip", detail="未知服务")
        report.services.append(result)

    # 计算整体状态
    error_count = sum(1 for s in report.services if s.status == "error")
    if error_count > 0:
        report.status = "unhealthy" if error_count == len(report.services) else "degraded"

    return report


def format_human(report: HealthReport) -> str:
    """格式化为人类可读的输出。"""
    lines = [f"健康检查报告 @ {report.timestamp}"]
    lines.append(f"整体状态: {report.status.upper()}")
    lines.append("─" * 50)
    for srv in report.services:
        icon = {"ok": "✅", "error": "❌", "skip": "⏭️"}
        detail = f" — {srv.detail}" if srv.detail else ""
        latency = f" [{srv.latency_ms:.0f}ms]" if srv.latency_ms else ""
        lines.append(f"  {icon.get(srv.status, '❓')} {srv.name}{latency}{detail}")
    lines.append("─" * 50)
    summary = report.summary
    lines.append(f"总计: {summary['total']} | ✅ {summary['ok']} | ❌ {summary['error']} | ⏭️ {summary['skip']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="系统健康检查")
    parser.add_argument(
        "--json", action="store_true",
        help="以 JSON 格式输出（供 Prometheus / 监控系统）",
    )
    parser.add_argument(
        "--service", type=str, default=None,
        help="只检查指定服务（如 redis、timescaledb、freqtrade_api）",
    )
    args = parser.parse_args()

    report = run_health_check(service_filter=args.service)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_human(report))

    return 0 if report.status in ("healthy", "degraded") else 1


if __name__ == "__main__":
    sys.exit(main())

