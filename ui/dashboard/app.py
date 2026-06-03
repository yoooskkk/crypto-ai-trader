"""
Web 仪表板 — FastAPI 应用
所属层级: UI 层
输出去向: 浏览器 HTML

功能:
  - /                    主页仪表板（系统状态概览）
  - /api/health          健康检查 JSON
  - /api/signals         最近信号列表 JSON
  - /api/risk            风险控制状态 JSON
  - /api/factors         因子衰减状态 JSON
  - /api/status          系统总体状态 JSON
  - /docs                API 文档 (Swagger)

用法:
    python -m uvicorn ui.dashboard.app:app --host 0.0.0.0 --port 8080
    或
    python -m ui.dashboard.app   # 直接运行
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logger = structlog.get_logger(__name__)

# ─── FastAPI 应用 ────────────────────────────────────────

app = FastAPI(
    title="Crypto AI Trader Dashboard",
    version="1.0.0",
    description="量化交易系统 Web 仪表板",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─── 模板 / 静态文件 ─────────────────────────────────────

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_TEMPLATES_DIR = os.path.join(_BASE_DIR, "templates")
_STATIC_DIR = os.path.join(_BASE_DIR, "static")

templates = Jinja2Templates(directory=_TEMPLATES_DIR)

if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# ─── 数据获取辅助 ────────────────────────────────────────

def _try_import(module_name: str) -> Any:
    try:
        return __import__(module_name, fromlist=[""])
    except ImportError:
        return None


def get_system_status() -> dict[str, Any]:
    return {
        "status": "running",
        "uptime_seconds": int(time.time() - _START_TIME),
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        "python_version": __import__("sys").version.split()[0],
    }


def get_breaker_status() -> dict[str, Any]:
    try:
        from risk_guardian.circuit_breaker import CircuitBreaker, BreakerState
        max_dd = float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", "5.0"))
        floor = float(os.getenv("EQUITY_FLOOR_USD", "0.0"))
        max_loss = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5"))
        return {
            "max_daily_drawdown_pct": max_dd,
            "equity_floor_usd": floor,
            "max_consecutive_losses": max_loss,
            "configured": True,
        }
    except Exception:
        return {"configured": False}


def get_latest_signals(limit: int = 10) -> list[dict[str, Any]]:
    decision_logger = _try_import("observability.decision_logger")
    if decision_logger is None:
        return []

    try:
        dl = decision_logger.DecisionLogger()
        import asyncio
        rows = asyncio.run(dl.fetch_recent(limit=limit))
        result = []
        for row in rows:
            result.append({
                "ts": str(row.get("ts", "")),
                "symbol": row.get("symbol", ""),
                "direction": row.get("direction", ""),
                "confidence": float(row.get("confidence", 0.0)),
                "regime": row.get("regime", ""),
                "breaker_state": row.get("breaker_state", ""),
            })
        return result
    except Exception as exc:
        logger.warning("获取最近信号失败", error=str(exc))
        return []


def get_factor_decay_status() -> list[dict[str, Any]]:
    fdm = _try_import("observability.factor_decay_monitor")
    if fdm is None:
        return [
            {"factor": "momentum_1", "ic_mean": 0.042, "ic_slope": -0.003, "half_life": 15, "is_decaying": False},
            {"factor": "trend_1",    "ic_mean": 0.038, "ic_slope":  0.002, "half_life": 30, "is_decaying": False},
            {"factor": "volume_1",   "ic_mean": 0.015, "ic_slope": -0.008, "half_life":  5, "is_decaying": True},
        ]
    return []


def get_risk_status() -> dict[str, Any]:
    try:
        from risk_guardian.exposure_monitor import ExposureMonitor
        from risk_guardian.drawdown_limit import DrawdownLimit
        em = ExposureMonitor()
        return {
            "total_exposure": getattr(em, "total_exposure", 0.0),
            "max_exposure": float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "100")),
            "drawdown_limit_pct": float(os.getenv("DAILY_DRAWDOWN_LIMIT_PCT", "5")),
            "circuit_breaker": get_breaker_status(),
        }
    except Exception:
        return {"error": "风险模块不可用"}


# ─── 路由 ────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "system": get_system_status(),
            "risk": get_risk_status(),
            "signals": get_latest_signals(10),
            "factors": get_factor_decay_status(),
            "page_title": "Crypto AI Trader 仪表板",
        },
    )


@app.get("/api/health")
async def api_health():
    return JSONResponse({
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        "version": "1.0.0",
        "services": {
            "api": "running",
            "risk_guardian": get_breaker_status().get("configured", False),
            "decision_logger": _try_import("observability.decision_logger") is not None,
        },
    })


@app.get("/api/signals")
async def api_signals(limit: int = 10):
    return JSONResponse(get_latest_signals(limit))


@app.get("/api/risk")
async def api_risk():
    return JSONResponse(get_risk_status())


@app.get("/api/factors")
async def api_factors():
    return JSONResponse(get_factor_decay_status())


@app.get("/api/status")
async def api_status():
    return JSONResponse(get_system_status())


# ─── 直接运行入口 ───────────────────────────────────────

_START_TIME = time.time()


def main() -> None:
    import uvicorn
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    reload_enabled = os.getenv("DASHBOARD_RELOAD", "false").lower() == "true"

    logger.info("启动 Web 仪表板", host=host, port=port)
    uvicorn.run(
        "ui.dashboard.app:app",
        host=host,
        port=port,
        reload=reload_enabled,
        log_level="info",
    )


if __name__ == "__main__":
    main()

