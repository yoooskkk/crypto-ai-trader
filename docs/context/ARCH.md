# ARCH.md — 架构速查卡（所有角色必读，约 600 token）

> 精简版架构文档。完整背景见 AI_CONTEXT.md（归档用，日常开发不必读）。

---

## 1. 系统定义

`crypto-ai-trader`：Binance 数据 + 多指标 + LLM AI 引擎 + Freqtrade，Docker Compose 一键部署的加密货币量化交易系统。

---

## 2. 层级与 Stream 流向

```
[ 数据采集 ]  data/          → 写入: raw_kline
[ 消息队列 ]  messaging/     → 基础设施层，不产生业务 Stream
[ 指标计算 ]  indicators/    → 消费: raw_kline        写入: indicators
[ 制度识别 ]  regime/        → 消费: indicators        写入: regime_signal
[ 分析层   ]  analysis/      → 消费: indicators + regime_signal（内部调用）
[ AI引擎   ]  ai_engine/     → 消费: regime_signal    写入: ai_signal
[ 风险控制 ]  risk_guardian/ → 消费: ai_signal         写入: trade_order
[ 策略执行 ]  freqtrade_strategies/ → 消费: trade_order
[ 横切     ]  observability/ · security/ → 所有层可写，不产生业务 Stream
```

Stream 名称常量（禁止修改）：
`raw_kline` · `indicators` · `regime_signal` · `ai_signal` · `trade_order`

---

## 3. 铁律（违反 = 严重错误，优先级高于一切需求）

1. `risk_guardian` 是**唯一**可调用 Freqtrade `force_exit` API 的模块
2. `factor_mining.py` 只能读 `validation/datasets/train/`，禁止碰 validate/ 和 oos/
3. `validation/datasets/oos/` 只用一次，用后作废
4. 密钥永不出现在日志、代码、Git 提交中
5. LLM 输出必须经 `schema_validator.py` 校验后才能流转
6. 所有指标参数从 `config/indicators.yml` 读取，代码中不硬编码数字
7. 服务间通信只通过 Redis Stream，禁止 HTTP 同步调用

---

## 4. 禁止清单

- 任何模块直接调用 Freqtrade API（除 risk_guardian）
- 密钥出现在 structlog 任何字段
- factor_mining.py 引入 validate/ 或 oos/ 路径
- 修改 Stream 名称常量
- LLM 输出绕过 schema_validator 直接触发订单
- 代码中硬编码指标参数数字

---

## 5. 代码规范（必须遵守）

```python
"""
模块名称
所属层级: [数据采集/消息队列/指标计算/制度识别/分析/AI引擎/风险控制/验证/执行]
输入来源: [Stream名 或 内部模块]
输出去向: [Stream名 或 返回值格式]
关键依赖: [内部模块列表]
"""
from __future__ import annotations
import structlog  # 唯一日志工具，禁止 print
```

- Python 3.11+，所有 IO 使用 `async/await`
- 类型注解必填，行宽 100 字符

---

## 6. 变更影响矩阵

| 变更 | 必须同步更新 |
|-----|------------|
| Stream 名称 | 所有上下游生产者/消费者 |
| TradePlan schema | schema_validator · plan_generator · strategy_adapter |
| risk.yml 熔断阈值 | test_circuit_breaker.py |
| indicators.yml 参数 | 无需改代码（自动读取）|

---

## 7. 市场制度联动

```
TRENDING       → 仓位上限 80%  · MACD/EMA 优先
RANGING        → 仓位上限 40%  · RSI/STOCH 优先 · 趋势信号关闭
HIGH_VOLATILITY → 仓位系数 ×0.5 · 收窄止损
UNKNOWN        → 仓位上限 20%  · 仅置信度 >0.8 通过
```

多周期防漂移：PRIMARY=1h，CONFIRM=[4h,1d]，FAST=[5m,15m]（仅入场时机）

---
*此文件由人类维护，AI 不得自行修改*
