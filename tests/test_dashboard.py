"""
Web dashboard test suite
Covers: FastAPI routes / API endpoints / data helpers / main entry
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ui.dashboard.app import app, get_system_status, get_risk_status, get_factor_decay_status

client = TestClient(app)


# ─── 数据获取函数测试 ─────────────────────────────────

class TestGetSystemStatus:
    def test_returns_dict(self):
        status = get_system_status()
        assert isinstance(status, dict)
        assert status["status"] == "running"
        assert "uptime_seconds" in status
        assert "timestamp" in status

    def test_timestamp_format(self):
        status = get_system_status()
        assert status["timestamp"].endswith("Z")


class TestGetRiskStatus:
    def test_returns_dict(self):
        risk = get_risk_status()
        assert isinstance(risk, dict)

    def test_contains_breaker(self):
        risk = get_risk_status()
        assert "circuit_breaker" in risk

    def test_fallback_on_import_error(self):
        with patch.dict("sys.modules", {"risk_guardian.exposure_monitor": None}):
            risk = get_risk_status()
            assert "error" in risk or "circuit_breaker" in risk


class TestGetFactorDecayStatus:
    def test_returns_list(self):
        factors = get_factor_decay_status()
        assert isinstance(factors, list)

    def test_has_expected_fields(self):
        factors = get_factor_decay_status()
        if factors:
            f = factors[0]
            assert "factor" in f
            assert "ic_mean" in f
            assert "ic_slope" in f
            assert "is_decaying" in f

    def test_has_decaying_status_field(self):
        factors = get_factor_decay_status()
        for f in factors:
            assert "is_decaying" in f


# ─── API 端点测试 ─────────────────────────────────────

class TestAPIHealth:
    def test_returns_200(self):
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_returns_json(self):
        resp = client.get("/api/health")
        data = resp.json()
        assert data["status"] == "ok"
        assert "services" in data

    def test_content_type(self):
        resp = client.get("/api/health")
        assert resp.headers["content-type"].startswith("application/json")


class TestAPISignals:
    def test_returns_200(self):
        resp = client.get("/api/signals")
        assert resp.status_code == 200

    def test_returns_list(self):
        resp = client.get("/api/signals")
        data = resp.json()
        assert isinstance(data, list)

    def test_limit_param(self):
        resp = client.get("/api/signals?limit=3")
        assert resp.status_code == 200


class TestAPIRisk:
    def test_returns_200(self):
        resp = client.get("/api/risk")
        assert resp.status_code == 200

    def test_returns_dict(self):
        resp = client.get("/api/risk")
        data = resp.json()
        assert isinstance(data, dict)


class TestAPIFactors:
    def test_returns_200(self):
        resp = client.get("/api/factors")
        assert resp.status_code == 200

    def test_returns_list(self):
        resp = client.get("/api/factors")
        data = resp.json()
        assert isinstance(data, list)

    def test_items_have_factor_field(self):
        resp = client.get("/api/factors")
        data = resp.json()
        if data:
            assert "factor" in data[0]


class TestAPIStatus:
    def test_returns_200(self):
        resp = client.get("/api/status")
        assert resp.status_code == 200

    def test_contains_uptime(self):
        resp = client.get("/api/status")
        data = resp.json()
        assert "uptime_seconds" in data
        assert data["uptime_seconds"] >= 0


# ─── 主页测试 ─────────────────────────────────────────

class TestIndexPage:
    def test_returns_200(self):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_returns_html(self):
        resp = client.get("/")
        assert resp.headers["content-type"].startswith("text/html")

    def test_contains_title(self):
        resp = client.get("/")
        content = resp.text
        assert "Crypto AI Trader" in content or "仪表板" in content

    def test_contains_system_status(self):
        resp = client.get("/")
        content = resp.text
        assert "running" in content or "RUNNING" in content or "状态" in content


# ─── 主入口测试 ─────────────────────────────────────────

class TestMain:
    def test_main_imports(self):
        """main() 导入不抛异常。"""
        from ui.dashboard.app import main
        assert main is not None

    def test_app_asgi(self):
        """app 是有效的 ASGI 应用。"""
        assert hasattr(app, "__call__")
        assert callable(app.__call__)


# ─── 错误处理测试 ──────────────────────────────────────

class TestErrorHandling:
    def test_404_returns_json(self):
        """不存在的路由应返回 404。"""
        resp = client.get("/api/nonexistent")
        assert resp.status_code == 404

    def test_invalid_limit(self):
        """无效的 limit 参数应优雅处理。"""
        resp = client.get("/api/signals?limit=-1")
        assert resp.status_code in (200, 422)  # FastAPI 验证或默认处理


# ─── 集成依赖测试（可选，不打真实服务） ─────────────

class TestIntegration:
    def test_no_real_db_fallback(self):
        """无数据库时 API 应返回空列表而非崩溃。"""
        with patch("ui.dashboard.app.get_latest_signals", return_value=[]):
            resp = client.get("/api/signals")
            data = resp.json()
            assert data == []
