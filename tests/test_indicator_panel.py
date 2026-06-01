"""
指标选择面板测试。
覆盖：配置加载、IndicatorConfig 模型、导出功能、面板渲染、交互流程。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from ui.cli.indicator_panel import (
    IndicatorConfig,
    export_config,
    load_indicator_config,
    pick_indicators,
    print_indicator_panel,
)


class TestLoadIndicatorConfig:
    """配置加载测试。"""

    def test_load_returns_dict(self):
        """加载 indicators.yml 应返回字典。"""
        cfg = load_indicator_config()
        assert isinstance(cfg, dict)
        assert "trend" in cfg
        assert "momentum" in cfg

    def test_load_contains_key_sections(self):
        """配置应包含所有主要类别。"""
        cfg = load_indicator_config()
        for section in ["trend", "momentum", "volatility", "volume"]:
            assert section in cfg, f"缺少配置段: {section}"


class TestIndicatorConfig:
    """IndicatorConfig 模型测试。"""

    def test_default_config_all_enabled(self):
        """默认配置下所有类别和指标应启用。"""
        config = IndicatorConfig()
        assert config.is_category_enabled("trend") is True
        assert config.is_category_enabled("momentum") is True

    def test_disabled_category_disables_indicators(self):
        """禁用类别后,其下的指标也应视为禁用。"""
        config = IndicatorConfig()
        config.categories["trend"] = False
        assert config.is_category_enabled("trend") is False
        assert config.is_indicator_enabled("trend", "ema_periods") is False

    def test_enabled_category_with_disabled_indicator(self):
        """类别启用但指标禁用应正确反映。"""
        config = IndicatorConfig()
        config.categories["momentum"] = True
        config.indicators["momentum"] = {
            "rsi_period": {"_enabled": False, "value": 14},
        }
        assert config.is_indicator_enabled("momentum", "rsi_period") is False

    def test_custom_category_defaults(self):
        """未在 categories 字典中的类别应视为启用。"""
        config = IndicatorConfig()
        assert config.is_category_enabled("unknown_cat") is True

    def test_indicators_default_to_empty(self):
        """未设置指标字典的类别,指标应启用（返回 True）。"""
        config = IndicatorConfig()
        config.categories["volume"] = True
        assert config.is_indicator_enabled("volume", "mfi_period") is True


class TestExportConfig:
    """导出配置测试。"""

    def test_export_basic(self):
        """基本导出应包含已启用的类别和指标。"""
        config = IndicatorConfig()
        config.categories["trend"] = True
        config.indicators["trend"] = {
            "ema_periods": {"_enabled": True, "value": [9, 21, 55, 200]},
            "adx_period": {"_enabled": True, "value": 14},
        }
        config.categories["momentum"] = False

        exported = export_config(config)
        assert "trend" in exported
        assert "momentum" not in exported
        assert exported["trend"]["ema_periods"] == [9, 21, 55, 200]
        assert exported["trend"]["adx_period"] == 14

    def test_export_disabled_indicator_excluded(self):
        """禁用的指标应从导出中排除。"""
        config = IndicatorConfig()
        config.categories["volume"] = True
        config.indicators["volume"] = {
            "mfi_period": {"_enabled": True, "value": 14},
            "cmf_period": {"_enabled": False, "value": 20},
        }
        exported = export_config(config)
        assert "mfi_period" in exported["volume"]
        assert "cmf_period" not in exported["volume"]

    def test_export_none_value_excluded(self):
        """值为 None 的指标应从导出中排除。"""
        config = IndicatorConfig()
        config.categories["trend"] = True
        config.indicators["trend"] = {
            "ema_periods": {"_enabled": True, "value": None},
        }
        exported = export_config(config)
        assert "ema_periods" not in exported.get("trend", {})

    def test_export_empty_config(self):
        """空配置导出应为空字典。"""
        config = IndicatorConfig()
        exported = export_config(config)
        assert isinstance(exported, dict)

    def test_export_disabled_category(self):
        """禁用整个类别时,其全部指标应排除。"""
        config = IndicatorConfig()
        config.categories["trend"] = False
        config.indicators["trend"] = {
            "ema_periods": {"_enabled": True, "value": [9, 21]},
        }
        exported = export_config(config)
        assert "trend" not in exported


class TestPrintIndicatorPanel:
    """面板渲染测试。"""

    def test_print_does_not_raise(self, capsys):
        """打印面板不应抛出异常。"""
        config = IndicatorConfig()
        print_indicator_panel(config)
        captured = capsys.readouterr()
        assert "指标配置面板" in captured.out

    def test_print_with_disabled_category(self, capsys):
        """禁用类别应在输出中显示。"""
        config = IndicatorConfig()
        config.categories["trend"] = False
        print_indicator_panel(config)
        captured = capsys.readouterr()
        assert "❌" in captured.out  # 禁用标记

    def test_print_with_enabled_category(self, capsys):
        """启用类别应在输出中显示。"""
        config = IndicatorConfig()
        config.categories["trend"] = True
        print_indicator_panel(config)
        captured = capsys.readouterr()
        assert "✅" in captured.out  # 启用标记


class TestPickIndicators:
    """交互式选择测试。"""

    def test_quit_immediately(self):
        """输入 'q' 应立即返回配置。"""
        with patch("builtins.input", side_effect=["q"]):
            config = pick_indicators()
            assert isinstance(config, IndicatorConfig)
            # 默认所有类别启用
            assert config.is_category_enabled("trend") is True

    def test_toggle_category(self):
        """切换类别状态应生效。"""
        with patch("builtins.input", side_effect=["1", "q"]):
            config = pick_indicators()
            # "1" 切换第一个类别 (trend) 的启用状态
            assert config.is_category_enabled("trend") is False

    def test_toggle_twice_returns_to_original(self):
        """两次切换回到原始状态。"""
        with patch("builtins.input", side_effect=["1", "1", "q"]):
            config = pick_indicators()
            # 两次切换 trend → 回到启用
            assert config.is_category_enabled("trend") is True

    def test_toggle_indicator(self):
        """切换指标状态应生效。"""
        with patch("builtins.input", side_effect=["1.1", "q"]):
            config = pick_indicators()
            # "1.1" 切换第一个类别的第一个指标 (trend.ema_periods) 的启用状态
            assert config.is_indicator_enabled("trend", "ema_periods") is False

    def test_reset_all(self):
        """重置（'r'）应恢复所有类别和指标为启用。"""
        with patch("builtins.input", side_effect=["1", "r", "q"]):
            config = pick_indicators()
            # 先禁用了 trend (1), 再重置 (r), 应恢复
            assert config.is_category_enabled("trend") is True


class TestMainEntryPoint:
    """CLI 入口测试。"""

    def test_main_executes(self):
        """main() 应可执行且不崩溃。"""
        from ui.cli.indicator_panel import main
        with patch("builtins.input", side_effect=["q"]):
            main()  # 不应抛出异常
