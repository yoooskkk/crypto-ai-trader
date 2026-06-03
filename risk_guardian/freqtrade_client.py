"""
Freqtrade REST API 客户端
所属层级: 风险控制层 (Risk Guardian)
调用端点: Freqtrade REST API (/api/v1/forceexit)
关键依赖: requests, os (env vars), structlog

仅限 risk_guardian 模块调用（铁律 #1）。
调用前后必须通过 decision_logger 记录。

用法:
    from risk_guardian.freqtrade_client import FreqtradeClient

    client = FreqtradeClient()
    result = client.force_exit_all()
    # 成功时 result["status"] == "success"
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ─── 默认配置（可通过环境变量覆盖）──────────────────────────

_DEFAULT_BASE_URL = os.getenv("FREQTRADE_API_URL", "http://freqtrade:8080")
_DEFAULT_USERNAME = os.getenv("FREQTRADE_USERNAME", "Freqtrader")
_DEFAULT_PASSWORD = os.getenv("FREQTRADE_PASSWORD", "")
_DEFAULT_TIMEOUT = int(os.getenv("FREQTRADE_API_TIMEOUT", "10"))


# ─── 常量 ──────────────────────────────────────────────────

_LOGIN_ENDPOINT = "/api/v1/token/login"
_FORCE_EXIT_ENDPOINT = "/api/v1/forceexit"
_STATUS_ENDPOINT = "/api/v1/status"
_COUNT_ENDPOINT = "/api/v1/count"


# ─── 数据模型 ──────────────────────────────────────────────

@dataclass
class ForceExitResult:
    """force_exit 调用结果。"""
    success: bool
    trade_id: int | str
    result: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "trade_id": self.trade_id,
            "result": self.result,
            "error": self.error,
        }


# ─── 客户端 ────────────────────────────────────────────────

class FreqtradeClient:
    """
    Freqtrade REST API 客户端。

    功能:
        - JWT 认证（自动登录 + 令牌缓存）
        - force_exit(trade_id) — 强平指定交易
        - force_exit_all() — 强平所有持仓
        - get_open_trades() — 查询当前持仓
        - get_open_trade_count() — 持仓数量

    环境变量:
        FREQTRADE_API_URL      默认 http://freqtrade:8080
        FREQTRADE_USERNAME     默认 Freqtrade
        FREQTRADE_PASSWORD     默认 (空)
        FREQTRADE_API_TIMEOUT  默认 10 (秒)

    用法:
        client = FreqtradeClient()
        result = client.force_exit_all()
        if result.success:
            logger.info("强平成功", trade_id=result.trade_id)
        else:
            logger.error("强平失败", error=result.error)
    """

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: int | None = None,
    ):
        """
        参数:
            base_url: Freqtrade API 基础 URL（含协议和端口）
            username: API 用户名
            password: API 密码
            timeout: 请求超时（秒）
        """
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._username = username or _DEFAULT_USERNAME
        self._password = password or _DEFAULT_PASSWORD
        self._timeout = timeout or _DEFAULT_TIMEOUT

        self._jwt_token: str | None = None
        self._token_expires_at: float = 0.0

        self._requests_available = False
        try:
            __import__("requests")
            self._requests_available = True
        except ImportError:
            logger.warning("requests 模块未安装，FreqtradeClient 将不可用")

    # ─── 公开方法 ────────────────────────────────────────

    def force_exit(self, trade_id: int | str = "all") -> ForceExitResult:
        """
        强平指定交易。

        参数:
            trade_id: 交易 ID（int），或 "all"（强平所有）

        返回:
            ForceExitResult
        """
        if not self._requests_available:
            return ForceExitResult(
                success=False,
                trade_id=trade_id,
                result="",
                error="requests 模块未安装",
            )

        token = self._get_token()
        if token is None:
            return ForceExitResult(
                success=False,
                trade_id=trade_id,
                result="",
                error="Freqtrade API 认证失败",
            )

        import requests as req

        url = f"{self._base_url}{_FORCE_EXIT_ENDPOINT}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {"tradeid": trade_id}

        try:
            logger.info(
                "调用 Freqtrade force_exit API",
                trade_id=trade_id,
                url=url,
            )

            resp = req.post(
                url,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )

            if resp.status_code == 200:
                data = resp.json()
                result_text = data.get("result", resp.text)
                logger.info(
                    "Freqtrade force_exit 成功",
                    trade_id=trade_id,
                    result=result_text[:200],
                )
                return ForceExitResult(
                    success=True,
                    trade_id=trade_id,
                    result=result_text,
                )
            elif resp.status_code == 401:
                # Token 可能过期，清空缓存后重试一次
                self._jwt_token = None
                logger.warning("Freqtrade API token 过期，尝试重新认证")
                return self.force_exit(trade_id=trade_id)
            else:
                error_msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
                logger.error(
                    "Freqtrade force_exit 失败",
                    trade_id=trade_id,
                    status=resp.status_code,
                    response=resp.text[:300],
                )
                return ForceExitResult(
                    success=False,
                    trade_id=trade_id,
                    result="",
                    error=error_msg,
                )

        except req.exceptions.Timeout:
            logger.error(
                "Freqtrade force_exit 超时",
                trade_id=trade_id,
                timeout=self._timeout,
            )
            return ForceExitResult(
                success=False,
                trade_id=trade_id,
                result="",
                error=f"请求超时（{self._timeout}s）",
            )
        except req.exceptions.ConnectionError as exc:
            logger.error(
                "Freqtrade 连接失败",
                url=self._base_url,
                error=str(exc),
            )
            return ForceExitResult(
                success=False,
                trade_id=trade_id,
                result="",
                error=f"连接失败: {exc}",
            )
        except Exception as exc:
            logger.error(
                "Freqtrade force_exit 异常",
                trade_id=trade_id,
                error=str(exc),
            )
            return ForceExitResult(
                success=False,
                trade_id=trade_id,
                result="",
                error=str(exc),
            )

    def force_exit_all(self) -> ForceExitResult:
        """强平所有持仓（tradeid="all"）。"""
        return self.force_exit(trade_id="all")

    def get_open_trades(self) -> list[dict[str, Any]]:
        """
        获取当前所有持仓。

        返回:
            持仓列表，API 不可用时返回空列表
        """
        if not self._requests_available:
            return []

        token = self._get_token()
        if token is None:
            return []

        import requests as req

        url = f"{self._base_url}{_STATUS_ENDPOINT}"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            resp = req.get(url, headers=headers, timeout=self._timeout)
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.warning(
                    "获取 Freqtrade 持仓失败",
                    status=resp.status_code,
                )
                return []
        except Exception as exc:
            logger.error("获取 Freqtrade 持仓异常", error=str(exc))
            return []

    def get_open_trade_count(self) -> int:
        """
        获取当前持仓数量。

        返回:
            持仓数，API 不可用时返回 0
        """
        if not self._requests_available:
            return 0

        token = self._get_token()
        if token is None:
            return 0

        import requests as req

        url = f"{self._base_url}{_COUNT_ENDPOINT}"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            resp = req.get(url, headers=headers, timeout=self._timeout)
            if resp.status_code == 200:
                data = resp.json()
                return int(data.get("current", 0))
            return 0
        except Exception:
            return 0

    def health_check(self) -> bool:
        """
        检查 Freqtrade API 是否可用。

        返回:
            True 表示 API 可正常响应
        """
        if not self._requests_available:
            return False

        import requests as req

        url = f"{self._base_url}/api/v1/ping"
        try:
            resp = req.get(url, timeout=min(self._timeout, 5))
            if resp.status_code == 200:
                data = resp.json()
                return data.get("status") == "pong"
            return False
        except Exception:
            return False

    # ─── 内部方法 ─────────────────────────────────────────

    def _get_token(self) -> str | None:
        """
        获取 JWT token。

        如果缓存有效则直接返回，否则重新登录。
        返回 None 表示认证失败。
        """
        if self._jwt_token and time.time() < self._token_expires_at:
            return self._jwt_token

        return self._login()

    def _login(self) -> str | None:
        """
        登录 Freqtrade API 获取 JWT token。

        POST /api/v1/token/login
        请求体: {"username": "...", "password": "..."}
        响应:   {"access_token": "xxx", "token_type": "Bearer"}

        返回:
            access_token 字符串，失败返回 None
        """
        if not self._requests_available:
            return None

        if not self._password:
            logger.error("FREQTRADE_PASSWORD 未设置，无法认证")
            return None

        import requests as req

        url = f"{self._base_url}{_LOGIN_ENDPOINT}"
        payload = {
            "username": self._username,
            "password": self._password,
        }

        try:
            logger.info("Freqtrade API 登录中", url=url)

            resp = req.post(
                url,
                json=payload,
                timeout=self._timeout,
            )

            if resp.status_code == 200:
                data = resp.json()
                token = data.get("access_token")
                if token:
                    self._jwt_token = token
                    # token 默认 30 分钟过期，提前 5 分钟刷新
                    self._token_expires_at = time.time() + 1500
                    logger.info("Freqtrade API 登录成功")
                    return token
                else:
                    logger.error("Freqtrade API 返回中无 access_token")
                    return None
            else:
                logger.error(
                    "Freqtrade API 登录失败",
                    status=resp.status_code,
                    response=resp.text[:200],
                )
                return None

        except req.exceptions.Timeout:
            logger.error("Freqtrade API 登录超时")
            return None
        except req.exceptions.ConnectionError as exc:
            logger.error("Freqtrade API 连接失败", error=str(exc))
            return None
        except Exception as exc:
            logger.error("Freqtrade API 登录异常", error=str(exc))
            return None

    def clear_token(self) -> None:
        """清空缓存的 JWT token（强制下次请求重新登录）。"""
        self._jwt_token = None
        self._token_expires_at = 0.0
        logger.debug("Freqtrade JWT token 缓存已清空")


__all__ = [
    "FreqtradeClient",
    "ForceExitResult",
]
