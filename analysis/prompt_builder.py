"""
模块名称: prompt_builder.py
所属层级: 分析层 (Analysis)
输入来源: indicators Stream + regime_signal Stream + multi_tf_trend
输出去向: str（Jinja2 渲染后的 Prompt）
关键依赖: Jinja2, prompt_versioner, multi_tf_trend

将指标 + 制度 + 多周期趋势 → Jinja2 Prompt。
每次修改 .j2 模板后必须调用 prompt_versioner.register() 更新版本。

修订记录:
- v1.0: 初始实现，Jinja2 模板渲染 + 版本注册
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from analysis.multi_tf_trend import build_trend_summary

logger = structlog.get_logger(__name__)

# ─── 常量 ─────────────────────────────────────────────────────

_PROMPT_DIR = Path(__file__).parent.parent / "config" / "llm_prompts"
_DEFAULT_TEMPLATE = "market_analysis.j2"


class PromptBuilder:
    """
    Prompt 构建器。

    用法:
        builder = PromptBuilder()
        prompt = await builder.build(indicators_by_tf, regime_signal)
    """

    def __init__(
        self,
        template_name: str = _DEFAULT_TEMPLATE,
        prompt_dir: str | Path | None = None,
    ):
        """
        初始化 PromptBuilder。

        参数:
            template_name: Jinja2 模板文件名
            prompt_dir: 模板目录路径，默认使用 config/llm_prompts/
        """
        self._template_name = template_name
        self._prompt_dir = Path(prompt_dir) if prompt_dir else _PROMPT_DIR

        if not self._prompt_dir.exists():
            logger.warning("Prompt 目录不存在", path=str(self._prompt_dir))

        self._env = Environment(
            loader=FileSystemLoader(str(self._prompt_dir)),
            autoescape=False,
        )

        # 验证模板存在
        try:
            self._template = self._env.get_template(template_name)
        except TemplateNotFound:
            logger.error(
                "Jinja2 模板未找到",
                template=template_name,
                directory=str(self._prompt_dir),
            )
            self._template = None

    async def build(
        self,
        indicators_by_tf: dict[str, dict[str, float]],
        regime_signal: dict[str, Any],
    ) -> str | None:
        """
        构建 LLM Prompt。

        参数:
            indicators_by_tf: {timeframe: {indicator_name: value}}
                - 至少包含 PRIMARY (1h) 时间框架的指标
                - 可选包含 CONFIRM (4h, 1d) 和 FAST (5m, 15m) 周期
            regime_signal: regime_signal Stream 消息
                - 需包含: regime, confidence, adx, bb_width
                - 可选包含: method, prev_regime

        返回:
            渲染后的 Prompt 字符串，失败返回 None
        """
        if self._template is None:
            logger.error("模板未加载，无法构建 Prompt")
            return None

        # 1. 提取制度信息
        regime = regime_signal.get("regime", "UNKNOWN")
        regime_confidence = regime_signal.get("confidence", 0.0)

        # 2. 提取 PRIMARY 时间框架的核心指标
        primary_indicators = indicators_by_tf.get("1h", indicators_by_tf.get(
            next(iter(indicators_by_tf), None), {}
        ))

        # 3. 构建多周期趋势摘要
        trend_summary = build_trend_summary(indicators_by_tf, regime)
        trends = trend_summary["trends"]

        # 4. 提取币圈增强因子（如果存在）
        crypto_alpha = self._extract_crypto_alpha(indicators_by_tf)

        # 5. 提取当前价格（从 PRIMARY 指标中获取，若存在 close）
        current_price = primary_indicators.get("close", 0.0)

        # 6. 渲染模板
        try:
            prompt = self._template.render(
                symbol=regime_signal.get("symbol", "UNKNOWN"),
                timeframe="1h",
                regime=regime,
                regime_confidence=regime_confidence,
                trends={
                    tf: {
                        "direction": data["direction"],
                        "strength": trend_summary["consensus"]["strength"]
                        if tf == trend_summary["primary"] else "WEAK",
                    }
                    for tf, data in trends.items()
                },
                indicators={
                    k: v for k, v in primary_indicators.items()
                    if k != "close"
                },
                funding_rate=crypto_alpha.get("funding_rate", 0.0),
                oi_delta=crypto_alpha.get("oi_delta", 0.0),
                cvd_delta=crypto_alpha.get("cvd_delta", 0.0),
                current_price=current_price,
                consensus_direction=trend_summary["consensus"]["direction"],
                consensus_strength=trend_summary["consensus"]["strength"],
                entry_bias=trend_summary.get("entry_bias"),
            )

            logger.debug("Prompt 构建成功", template=self._template_name)
            return prompt

        except Exception as exc:
            logger.error("Prompt 渲染失败", error=str(exc), template=self._template_name)
            return None

    @staticmethod
    def _extract_crypto_alpha(
        indicators_by_tf: dict[str, dict[str, float]],
    ) -> dict[str, float]:
        """
        从指标数据中提取币圈增强因子。
        这些因子可能在任何时间框架的指标中，优先从 PRIMARY 中提取。
        """
        alpha_keys = ["funding_rate", "oi_delta", "cvd_delta"]
        result: dict[str, float] = {}

        primary = indicators_by_tf.get("1h", {})
        for key in alpha_keys:
            val = primary.get(key)
            if val is not None:
                result[key] = val

        # 如果在 PRIMARY 中没找到，从其他周期找
        if len(result) < len(alpha_keys):
            for tf in indicators_by_tf.values():
                for key in alpha_keys:
                    if key not in result:
                        val = tf.get(key)
                        if val is not None:
                            result[key] = val

        # 设置默认值
        for key in alpha_keys:
            result.setdefault(key, 0.0)

        return result

    @property
    def template_name(self) -> str:
        return self._template_name


__all__ = ["PromptBuilder"]
