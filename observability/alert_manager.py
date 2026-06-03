"""
告警管理器 — 多渠道通知
所属层级: 可观测性层 (Observability)
触发来源: circuit_breaker / drawdown_limit / processor / fallback_handler 等
输出去向: Telegram / Slack / 控制台日志
关键依赖: requests (可选), structlog

用法:
    from observability.alert_manager import alert_manager

    # 直接使用全局实例（懒初始化）
    await alert_manager.critical("熔断器触发", "BTCUSDT 日回撤 8.2%")
    await alert_manager.warning("AI 降级", "连续 3 次 LLM 调用失败，使用上次信号")
    await alert_manager.info("系统启动", "risk-guardian worker 已就绪")

环境变量:
    ALERT_TELEGRAM_BOT_TOKEN   Telegram Bot Token（可选，不设则禁用 Telegram）
    ALERT_TELEGRAM_CHAT_ID     Telegram 聊天/群组 ID
    ALERT_SLACK_WEBHOOK_URL    Slack Webhook URL（可选，不设则禁用 Slack）
    ALERT_MIN_LEVEL            最低告警级别（默认 "INFO"，可选 "WARNING"/"CRITICAL"）
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)


# ─── 告警级别 ────────────────────────────────────────────────

class AlertLevel(str, Enum):
    """告警级别（升序排列）。"""
    INFO     = "INFO"
    WARNING  = "WARNING"
    CRITICAL = "CRITICAL"


# ─── 配置 ────────────────────────────────────────────────────

_TELEGRAM_BOT_TOKEN  = os.getenv("ALERT_TELEGRAM_BOT_TOKEN", "")
_TELEGRAM_CHAT_ID    = os.getenv("ALERT_TELEGRAM_CHAT_ID", "")
_SLACK_WEBHOOK_URL   = os.getenv("ALERT_SLACK_WEBHOOK_URL", "")
_MIN_LEVEL           = os.getenv("ALERT_MIN_LEVEL", "INFO").upper()

_LEVEL_ORDER = {
    AlertLevel.INFO: 0,
    AlertLevel.WARNING: 1,
    AlertLevel.CRITICAL: 2,
}

# ─── 速率限制 ─────────────────────────────────────────────────

_RATE_LIMIT_WINDOW = 60.0  # 同一级别+摘要的告警 60 秒内不重复发送


# ─── 告警消息模型 ─────────────────────────────────────────────

@dataclass
class AlertMessage:
    """单条告警消息。"""
    level: AlertLevel
    title: str
    detail: str = ""
    symbol: str = ""
    ts: float = field(default_factory=time.time)
    tags: dict[str, Any] = field(default_factory=dict)

    def format_telegram(self) -> str:
        """格式化为 Telegram 消息（支持 MarkdownV2）。"""
        icon = {"INFO": "ℹ️", "WARNING": "⚠️", "CRITICAL": "🚨"}
        lines = [
            f"{icon.get(self.level.value, '📢')} *{self._escape_md(self.title)}*",
            f"级别: `{self.level.value}`",
        ]
        if self.symbol:
            lines.append(f"交易对: `{self.symbol}`")
        if self.detail:
            lines.append(f"详情: {self._escape_md(self.detail)}")
        if self.tags:
            for k, v in self.tags.items():
                lines.append(f"`{k}`: {self._escape_md(str(v))}")
        return "\n".join(lines)

    def format_slack(self) -> dict[str, Any]:
        """格式化为 Slack Block Kit 消息。"""
        color = {"INFO": "#36a64f", "WARNING": "#ffcc00", "CRITICAL": "#ff0000"}
        fields = [
            {"type": "mrkdwn", "text": f"*级别:*\n{self.level.value}"},
        ]
        if self.symbol:
            fields.append({"type": "mrkdwn", "text": f"*交易对:*\n{self.symbol}"})
        if self.detail:
            fields.append({"type": "mrkdwn", "text": f"*详情:*\n{self.detail}"})
        for k, v in self.tags.items():
            fields.append({"type": "mrkdwn", "text": f"*{k}:*\n{str(v)[:100]}"})

        return {
            "attachments": [{
                "color": color.get(self.level.value, "#cccccc"),
                "title": self.title,
                "fields": fields,
                "ts": int(self.ts),
            }]
        }

    def format_console(self) -> dict[str, Any]:
        """格式化为 structlog 结构化日志。"""
        return {
            "alert": self.title,
            "level": self.level.value,
            "symbol": self.symbol,
            "detail": self.detail,
            "tags": self.tags,
            "ts": self.ts,
        }

    @staticmethod
    def _escape_md(text: str) -> str:
        """转义 Telegram MarkdownV2 特殊字符。"""
        special = r"_*[]()~`>#+-=|{}.!"
        for ch in special:
            text = text.replace(ch, f"\\{ch}")
        return text


# ─── 通道接口 ────────────────────────────────────────────────

class AlertChannel:
    """告警通道基类。"""

    def __init__(self, name: str) -> None:
        self._name = name
        self._enabled = True
        self._last_sent: dict[str, float] = {}  # key -> timestamp

    @property
    def name(self) -> str:
        return self._name

    @property
    def enabled(self) -> bool:
        return self._enabled

    def disable(self) -> None:
        self._enabled = False

    def enable(self) -> None:
        self._enabled = True

    def is_rate_limited(self, key: str) -> bool:
        """检查是否触发速率限制。同一 key 在窗口内只发送一次。"""
        now = time.time()
        last = self._last_sent.get(key, 0.0)
        if now - last < _RATE_LIMIT_WINDOW:
            return True
        self._last_sent[key] = now
        return False

    def should_send(self, level: AlertLevel) -> bool:
        """根据最低配置级别判断是否发送。"""
        min_order = _LEVEL_ORDER.get(AlertLevel(_MIN_LEVEL), 0)
        current_order = _LEVEL_ORDER.get(level, 0)
        return self._enabled and current_order >= min_order

    async def send(self, message: AlertMessage) -> bool:
        """发送告警。子类需实现。返回 True 表示发送成功。"""
        raise NotImplementedError


# ─── Telegram 通道 ──────────────────────────────────────────

class TelegramChannel(AlertChannel):
    """Telegram Bot API 告警通道。"""

    _BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str, chat_id: str) -> None:
        super().__init__("telegram")
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._requests_available = False
        try:
            __import__("requests")
            self._requests_available = True
        except ImportError:
            logger.warning("requests 未安装，Telegram 通道不可用")

        if not bot_token or not chat_id:
            logger.warning("Telegram 配置不完整（token 或 chat_id 为空），通道禁用")
            self.disable()

    async def send(self, message: AlertMessage) -> bool:
        if not self._enabled or not self._requests_available:
            return False

        rate_key = f"{message.level.value}:{message.title}"
        if self.is_rate_limited(rate_key):
            logger.debug("Telegram 速率限制跳过", title=message.title)
            return False

        import requests as req

        url = self._BASE_URL.format(token=self._bot_token)
        payload = {
            "chat_id": self._chat_id,
            "text": message.format_telegram(),
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }

        try:
            resp = req.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                logger.debug("Telegram 告警已发送", title=message.title)
                return True
            else:
                logger.warning(
                    "Telegram 发送失败",
                    status=resp.status_code,
                    response=resp.text[:200],
                )
                return False
        except Exception as exc:
            logger.warning("Telegram 发送异常", error=str(exc))
            return False


# ─── Slack 通道 ─────────────────────────────────────────────

class SlackChannel(AlertChannel):
    """Slack Webhook 告警通道。"""

    def __init__(self, webhook_url: str) -> None:
        super().__init__("slack")
        self._webhook_url = webhook_url
        self._requests_available = False
        try:
            __import__("requests")
            self._requests_available = True
        except ImportError:
            logger.warning("requests 未安装，Slack 通道不可用")

        if not webhook_url:
            logger.warning("Slack Webhook URL 为空，通道禁用")
            self.disable()

    async def send(self, message: AlertMessage) -> bool:
        if not self._enabled or not self._requests_available:
            return False

        rate_key = f"{message.level.value}:{message.title}"
        if self.is_rate_limited(rate_key):
            return False

        import requests as req

        try:
            resp = req.post(
                self._webhook_url,
                json=message.format_slack(),
                timeout=10,
            )
            if resp.status_code in (200, 204):
                logger.debug("Slack 告警已发送", title=message.title)
                return True
            else:
                logger.warning(
                    "Slack 发送失败",
                    status=resp.status_code,
                )
                return False
        except Exception as exc:
            logger.warning("Slack 发送异常", error=str(exc))
            return False


# ─── 控制台通道（始终启用）─────────────────────────────────

class ConsoleChannel(AlertChannel):
    """控制台日志告警通道（始终启用，永不失败）。"""

    def __init__(self) -> None:
        super().__init__("console")

    async def send(self, message: AlertMessage) -> bool:
        log_data = message.format_console()
        if message.level == AlertLevel.CRITICAL:
            logger.critical("ALERT", **log_data)
        elif message.level == AlertLevel.WARNING:
            logger.warning("ALERT", **log_data)
        else:
            logger.info("ALERT", **log_data)
        return True


# ─── 告警管理器 ─────────────────────────────────────────────

class AlertManager:
    """
    多渠道告警管理器。

    用法:
        am = AlertManager()
        am.add_channel(TelegramChannel(token, chat_id))
        am.add_channel(SlackChannel(webhook_url))

        # 快捷方法
        await am.info("标题", "详情", symbol="BTCUSDT")
        await am.warning("标题", "详情", tags={"key": "val"})
        await am.critical("标题", "详情")
    """

    def __init__(self) -> None:
        self._channels: list[AlertChannel] = []
        self._console = ConsoleChannel()
        # 自动注册控制台通道
        self._channels.append(self._console)

        # 自动注册 Telegram（如果配置了环境变量）
        if _TELEGRAM_BOT_TOKEN and _TELEGRAM_CHAT_ID:
            self.add_channel(TelegramChannel(_TELEGRAM_BOT_TOKEN, _TELEGRAM_CHAT_ID))

        # 自动注册 Slack（如果配置了环境变量）
        if _SLACK_WEBHOOK_URL:
            self.add_channel(SlackChannel(_SLACK_WEBHOOK_URL))

    def add_channel(self, channel: AlertChannel) -> None:
        """添加告警通道。"""
        self._channels.append(channel)
        logger.info("告警通道已注册", channel=channel.name)

    def remove_channel(self, name: str) -> None:
        """移除告警通道。"""
        self._channels = [c for c in self._channels if c.name != name]

    async def send(
        self,
        level: str | AlertLevel,
        title: str,
        detail: str = "",
        symbol: str = "",
        tags: dict[str, Any] | None = None,
    ) -> dict[str, bool]:
        """
        发送告警到所有已注册通道。

        参数:
            level: 告警级别（INFO / WARNING / CRITICAL）
            title: 标题（简短描述）
            detail: 详细描述
            symbol: 相关交易对（可选）
            tags: 附加标签（可选）

        返回:
            {channel_name: success_bool, ...}
        """
        if isinstance(level, str):
            level = AlertLevel(level.upper())

        message = AlertMessage(
            level=level,
            title=title,
            detail=detail,
            symbol=symbol,
            tags=tags or {},
        )

        results: dict[str, bool] = {}
        tasks = []

        for channel in self._channels:
            if not channel.should_send(level):
                results[channel.name] = False
                continue
            tasks.append(self._send_to_channel(channel, message, results))

        if tasks:
            await asyncio.gather(*tasks)

        return results

    async def _send_to_channel(
        self,
        channel: AlertChannel,
        message: AlertMessage,
        results: dict[str, bool],
    ) -> None:
        try:
            ok = await channel.send(message)
            results[channel.name] = ok
        except Exception as exc:
            logger.error("告警通道发送异常", channel=channel.name, error=str(exc))
            results[channel.name] = False

    # ─── 快捷方法 ─────────────────────────────────────────

    async def info(
        self,
        title: str,
        detail: str = "",
        symbol: str = "",
        tags: dict[str, Any] | None = None,
    ) -> dict[str, bool]:
        """发送 INFO 级别告警。"""
        return await self.send(AlertLevel.INFO, title, detail, symbol, tags)

    async def warning(
        self,
        title: str,
        detail: str = "",
        symbol: str = "",
        tags: dict[str, Any] | None = None,
    ) -> dict[str, bool]:
        """发送 WARNING 级别告警。"""
        return await self.send(AlertLevel.WARNING, title, detail, symbol, tags)

    async def critical(
        self,
        title: str,
        detail: str = "",
        symbol: str = "",
        tags: dict[str, Any] | None = None,
    ) -> dict[str, bool]:
        """发送 CRITICAL 级别告警。"""
        return await self.send(AlertLevel.CRITICAL, title, detail, symbol, tags)


# ─── 全局实例（懒初始化）──────────────────────────────────

_alert_manager: AlertManager | None = None


def get_alert_manager() -> AlertManager:
    """
    获取全局 AlertManager 实例（懒初始化）。

    环境变量自动注册 Telegram 和 Slack 通道。
    """
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager


# 快捷引用（推荐用法）
alert_manager = get_alert_manager()


__all__ = [
    "AlertLevel",
    "AlertMessage",
    "AlertChannel",
    "TelegramChannel",
    "SlackChannel",
    "ConsoleChannel",
    "AlertManager",
    "get_alert_manager",
    "alert_manager",
]
