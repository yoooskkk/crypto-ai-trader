"""
AlertManager 测试套件
覆盖：AlertMessage 格式化 / AlertChannel 基类 / ConsoleChannel / AlertManager
      TelegramChannel 和 SlackChannel 使用 mock 避免真实 HTTP 调用
"""
from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, patch

import pytest

import observability
from observability.alert_manager import (
    AlertLevel,
    AlertMessage,
    AlertChannel,
    ConsoleChannel,
    AlertManager,
    get_alert_manager,
)


# ─── 固定装置 ─────────────────────────────────────────────────

@pytest.fixture
def info_msg() -> AlertMessage:
    return AlertMessage(
        level=AlertLevel.INFO,
        title="测试信息",
        detail="这是一个测试信息",
        symbol="BTCUSDT",
        tags={"source": "test"},
    )


@pytest.fixture
def warning_msg() -> AlertMessage:
    return AlertMessage(
        level=AlertLevel.WARNING,
        title="测试警告",
        detail="这是一个测试警告",
        symbol="ETHUSDT",
    )


@pytest.fixture
def critical_msg() -> AlertMessage:
    return AlertMessage(
        level=AlertLevel.CRITICAL,
        title="测试严重",
        detail="这是一个测试严重告警",
        tags={"reason": "drawdown"},
    )


# ─── AlertMessage 格式化测试 ────────────────────────────────

class TestAlertMessage:
    """AlertMessage 格式化逻辑测试。"""

    def test_format_console_contains_all_fields(self, info_msg: AlertMessage):
        result = info_msg.format_console()
        assert result["alert"] == "测试信息"
        assert result["level"] == "INFO"
        assert result["symbol"] == "BTCUSDT"
        assert result["detail"] == "这是一个测试信息"
        assert "source" in result["tags"]
        assert "ts" in result

    def test_format_telegram_contains_level(self, info_msg: AlertMessage):
        result = info_msg.format_telegram()
        assert "INFO" in result
        assert "BTCUSDT" in result
        assert "测试信息" in result

    def test_format_telegram_without_symbol(self):
        msg = AlertMessage(level=AlertLevel.WARNING, title="无交易对")
        result = msg.format_telegram()
        assert '交易对:' not in result

    def test_format_telegram_escapes_special_chars(self):
        msg = AlertMessage(level=AlertLevel.INFO, title="_test_ [danger]")
        result = msg.format_telegram()
        assert "\\_test\\_" in result
        assert "\\[danger\\]" in result

    def test_format_slack_contains_attachment(self, critical_msg: AlertMessage):
        result = critical_msg.format_slack()
        assert "attachments" in result
        assert len(result["attachments"]) == 1
        attach = result["attachments"][0]
        assert attach["color"] == "#ff0000"
        assert attach["title"] == "测试严重"

    def test_format_slack_fields(self, info_msg: AlertMessage):
        result = info_msg.format_slack()
        fields = result["attachments"][0]["fields"]
        field_texts = [f["text"] for f in fields]
        assert any("INFO" in f for f in field_texts)
        assert any("BTCUSDT" in f for f in field_texts)
        assert any("这是一个测试信息" in f for f in field_texts)

    def test_escape_md_handles_all_special_chars(self):
        text = r"_*[]()~`>#+-=|{}.!"
        escaped = AlertMessage._escape_md(text)
        assert escaped == r"\_\*\[\]\(\)\~\`\>\#\+\-\=\|\{\}\.\!"
        # 每个字符前面都有一个反斜杠
        assert len(escaped) == len(text) * 2

    def test_level_order_consistency(self):
        """INFO < WARNING < CRITICAL 的等级顺序正确。"""
        from observability.alert_manager import _LEVEL_ORDER
        assert _LEVEL_ORDER[AlertLevel.INFO] < _LEVEL_ORDER[AlertLevel.WARNING]
        assert _LEVEL_ORDER[AlertLevel.WARNING] < _LEVEL_ORDER[AlertLevel.CRITICAL]


# ─── AlertChannel 基类测试 ─────────────────────────────────

class TestAlertChannel:
    """AlertChannel 基类行为测试。"""

    @pytest.fixture
    def channel(self):
        return AlertChannel("test_channel")

    def test_name_property(self, channel: AlertChannel):
        assert channel.name == "test_channel"

    def test_enabled_default(self, channel: AlertChannel):
        assert channel.enabled is True

    def test_disable(self, channel: AlertChannel):
        channel.disable()
        assert channel.enabled is False

    def test_enable_after_disable(self, channel: AlertChannel):
        channel.disable()
        channel.enable()
        assert channel.enabled is True

    def test_rate_limit_first_call(self, channel: AlertChannel):
        assert channel.is_rate_limited("test_key") is False

    def test_rate_limit_second_call(self, channel: AlertChannel):
        channel.is_rate_limited("test_key")
        assert channel.is_rate_limited("test_key") is True

    def test_rate_limit_different_keys(self, channel: AlertChannel):
        channel.is_rate_limited("key_a")
        assert channel.is_rate_limited("key_b") is False

    def test_send_raises_not_implemented(self, channel: AlertChannel):
        msg = AlertMessage(level=AlertLevel.INFO, title="测试")
        with pytest.raises(NotImplementedError):
            # 需要事件循环来运行 async 方法
            import asyncio
            asyncio.run(channel.send(msg))

    def test_should_send_default(self, channel: AlertChannel):
        # 默认 ALERT_MIN_LEVEL=INFO，INFO 级别应该发送
        assert channel.should_send(AlertLevel.INFO) is True
        assert channel.should_send(AlertLevel.WARNING) is True
        assert channel.should_send(AlertLevel.CRITICAL) is True

    def test_should_send_disabled(self, channel: AlertChannel):
        channel.disable()
        assert channel.should_send(AlertLevel.CRITICAL) is False

    @pytest.mark.parametrize("min_level,level,expected", [
        ("INFO", AlertLevel.INFO, True),
        ("INFO", AlertLevel.WARNING, True),
        ("INFO", AlertLevel.CRITICAL, True),
        ("WARNING", AlertLevel.INFO, False),
        ("WARNING", AlertLevel.WARNING, True),
        ("WARNING", AlertLevel.CRITICAL, True),
        ("CRITICAL", AlertLevel.INFO, False),
        ("CRITICAL", AlertLevel.WARNING, False),
        ("CRITICAL", AlertLevel.CRITICAL, True),
    ])
    def test_should_send_min_level(
        self, min_level: str, level: AlertLevel, expected: bool,
    ):
        import observability.alert_manager as am
        old_value = am._MIN_LEVEL
        try:
            am._MIN_LEVEL = min_level
            ch = am.AlertChannel("test_min")
            assert ch.should_send(level) is expected
        finally:
            am._MIN_LEVEL = old_value


# ─── ConsoleChannel 测试 ──────────────────────────────────

class TestConsoleChannel:
    """ConsoleChannel 行为测试。"""

    @pytest.fixture
    def console(self) -> ConsoleChannel:
        return ConsoleChannel()

    def test_channel_name(self, console: ConsoleChannel):
        assert console.name == "console"

    def test_always_enabled(self, console: ConsoleChannel):
        assert console.enabled is True

    @pytest.mark.asyncio
    async def test_send_info_returns_true(self, console: ConsoleChannel, info_msg: AlertMessage):
        result = await console.send(info_msg)
        assert result is True

    @pytest.mark.asyncio
    async def test_send_warning_returns_true(self, console: ConsoleChannel, warning_msg: AlertMessage):
        result = await console.send(warning_msg)
        assert result is True

    @pytest.mark.asyncio
    async def test_send_critical_returns_true(self, console: ConsoleChannel, critical_msg: AlertMessage):
        result = await console.send(critical_msg)
        assert result is True

    @pytest.mark.asyncio
    async def test_console_never_fails(self, console: ConsoleChannel):
        """ConsoleChannel.send 应该永不抛出异常。"""
        msg = AlertMessage(level=AlertLevel.CRITICAL, title="test", detail="a" * 10000)
        result = await console.send(msg)
        assert result is True


# ─── TelegramChannel 测试（使用 mock）──────────────────────

class TestTelegramChannel:
    """TelegramChannel 测试（所有 HTTP 调用使用 mock）。"""

    @pytest.fixture
    def channel(self):
        from observability.alert_manager import TelegramChannel
        return TelegramChannel(bot_token="test:token", chat_id="12345")

    def test_channel_name(self, channel):
        assert channel.name == "telegram"

    def test_disabled_when_no_token(self):
        from observability.alert_manager import TelegramChannel
        ch = TelegramChannel(bot_token="", chat_id="12345")
        assert ch.enabled is False

    def test_disabled_when_no_chat_id(self):
        from observability.alert_manager import TelegramChannel
        ch = TelegramChannel(bot_token="test:token", chat_id="")
        assert ch.enabled is False

    @pytest.mark.asyncio
    async def test_send_success(self, channel):
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.text = '{"ok": true}'
            msg = AlertMessage(level=AlertLevel.INFO, title="测试")
            result = await channel.send(msg)
            assert result is True
            mock_post.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_http_error(self, channel):
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 400
            mock_post.return_value.text = "Bad Request"
            msg = AlertMessage(level=AlertLevel.INFO, title="测试")
            result = await channel.send(msg)
            assert result is False

    @pytest.mark.asyncio
    async def test_send_exception(self, channel):
        with patch("requests.post", side_effect=Exception("Connection failed")):
            msg = AlertMessage(level=AlertLevel.INFO, title="测试")
            result = await channel.send(msg)
            assert result is False

    @pytest.mark.asyncio
    async def test_disabled_returns_false(self):
        from observability.alert_manager import TelegramChannel
        ch = TelegramChannel(bot_token="", chat_id="")
        msg = AlertMessage(level=AlertLevel.INFO, title="测试")
        result = await ch.send(msg)
        assert result is False

    @pytest.mark.asyncio
    async def test_rate_limiting(self, channel):
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.text = '{"ok": true}'
            msg = AlertMessage(level=AlertLevel.INFO, title="速率测试")
            # 第一次发送应成功
            result1 = await channel.send(msg)
            assert result1 is True
            # 第二次相同 key 应被速率限制
            result2 = await channel.send(msg)
            assert result2 is False
            mock_post.assert_called_once()  # 只调用了一次


# ─── SlackChannel 测试（使用 mock）──────────────────────────

class TestSlackChannel:
    """SlackChannel 测试（所有 HTTP 调用使用 mock）。"""

    @pytest.fixture
    def channel(self):
        from observability.alert_manager import SlackChannel
        return SlackChannel(webhook_url="https://hooks.slack.com/test")

    def test_channel_name(self, channel):
        assert channel.name == "slack"

    def test_disabled_when_no_url(self):
        from observability.alert_manager import SlackChannel
        ch = SlackChannel(webhook_url="")
        assert ch.enabled is False

    @pytest.mark.asyncio
    async def test_send_success(self, channel):
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            msg = AlertMessage(level=AlertLevel.WARNING, title="Slack测试")
            result = await channel.send(msg)
            assert result is True
            mock_post.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_http_error(self, channel):
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 500
            msg = AlertMessage(level=AlertLevel.WARNING, title="Slack失败")
            result = await channel.send(msg)
            assert result is False

    @pytest.mark.asyncio
    async def test_send_exception(self, channel):
        with patch("requests.post", side_effect=TimeoutError("timeout")):
            msg = AlertMessage(level=AlertLevel.WARNING, title="Slack超时")
            result = await channel.send(msg)
            assert result is False

    @pytest.mark.asyncio
    async def test_disabled_returns_false(self):
        from observability.alert_manager import SlackChannel
        ch = SlackChannel(webhook_url="")
        msg = AlertMessage(level=AlertLevel.INFO, title="测试")
        result = await ch.send(msg)
        assert result is False


# ─── AlertManager 综合测试 ────────────────────────────────

class TestAlertManager:
    """AlertManager 编排逻辑测试。"""

    def test_init_always_has_console(self):
        """AlertManager 初始化时总应有控制台通道。"""
        am = AlertManager()
        names = [c.name for c in am._channels]
        assert "console" in names

    def test_add_channel(self):
        am = AlertManager()
        ch = AlertChannel("custom")
        am.add_channel(ch)
        names = [c.name for c in am._channels]
        assert "custom" in names

    def test_remove_channel(self):
        am = AlertManager()
        ch = AlertChannel("temp")
        am.add_channel(ch)
        am.remove_channel("temp")
        names = [c.name for c in am._channels]
        assert "temp" not in names

    @pytest.mark.asyncio
    async def test_send_info_returns_dict(self):
        """send() 应返回 {channel: bool} 字典。"""
        am = AlertManager()
        results = await am.info("测试信息", detail="详情", symbol="BTCUSDT")
        assert isinstance(results, dict)
        assert "console" in results
        assert results["console"] is True

    @pytest.mark.asyncio
    async def test_send_critical_returns_dict(self):
        am = AlertManager()
        results = await am.critical("测试严重", tags={"reason": "dd"})
        assert isinstance(results, dict)
        assert results["console"] is True

    @pytest.mark.asyncio
    async def test_send_warning_with_tags(self):
        am = AlertManager()
        results = await am.warning("测试警告", detail="回撤警告", tags={"dd_pct": "8.5%"})
        assert results["console"] is True

    @pytest.mark.asyncio
    async def test_send_unknown_level_fallback(self):
        """未知级别字符串应触发错误。"""
        am = AlertManager()
        with pytest.raises(ValueError):
            await am.send("UNKNOWN", "测试")

    @pytest.mark.asyncio
    async def test_multiple_channels_parallel(self):
        """多个通道应并行发送。"""
        from observability.alert_manager import TelegramChannel, SlackChannel
        am = AlertManager()
        # 添加 mock 通道
        tg = TelegramChannel(bot_token="test:token", chat_id="123")
        sl = SlackChannel(webhook_url="https://hooks.slack.com/test")

        # Mock HTTP 调用
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.text = '{"ok": true}'

            am.add_channel(tg)
            am.add_channel(sl)

            results = await am.critical("多渠道测试")
            assert results["console"] is True

    @pytest.mark.asyncio
    async def test_channel_exception_does_not_break_others(self):
        """单个通道异常不应影响其他通道。"""
        am = AlertManager()

        class BrokenChannel(AlertChannel):
            async def send(self, message: AlertMessage) -> bool:
                raise RuntimeError("Broken")

        broken = BrokenChannel("broken")
        am.add_channel(broken)

        results = await am.critical("异常测试")
        assert results["console"] is True
        assert results["broken"] is False

    def test_get_alert_manager_singleton(self):
        """get_alert_manager() 应返回同一实例。"""
        am1 = get_alert_manager()
        am2 = get_alert_manager()
        assert am1 is am2


# ─── 模块级功能测试 ───────────────────────────────────────

class TestModuleLevel:
    """模块级功能测试。"""

    def test_alert_manager_importable(self):
        from observability.alert_manager import alert_manager
        assert alert_manager is not None

    def test_alert_level_enum_values(self):
        assert AlertLevel.INFO.value == "INFO"
        assert AlertLevel.WARNING.value == "WARNING"
        assert AlertLevel.CRITICAL.value == "CRITICAL"
