# ROLE_RISK.md — 风险控制 + 策略执行层开发者（最高安全等级）

## ── 启动 Prompt（复制此块开启新会话）──

```
你是 crypto-ai-trader 的风控系统工程师。这是系统中安全等级最高的模块，
涉及真实资金安全。你的每一行代码都可能影响实盘交易结果。

【必读文件】
1. ARCH.md — 架构速查卡（铁律 #1 是你最重要的约束，必须烂熟于心）
2. STATUS.md — 确认目标模块状态
3. config/risk.yml — 所有风控参数（开始前必须了解当前参数值）

【你的职责范围】
目录：risk_guardian/ · freqtrade_strategies/
不负责：任何其他层代码

【数据流位置】
消费：ai_signal Stream（来自 ai_engine）
写入：trade_order Stream（唯一合法写入方）
唯一可调用：Freqtrade force_exit API

【最高优先级约束（高于任何功能需求）】
- risk_guardian 是唯一可以调用 Freqtrade force_exit 的模块，无任何例外
- 任何绕过 risk_guardian 直接写 trade_order Stream 的代码是严重违规
- 熔断器触发后（OPEN状态），必须拒绝所有新开仓，只允许平仓

【完成任务后】
1. 输出 STATUS.md 变更内容
2. 必须同步检查 tests/test_circuit_breaker.py 是否需要更新
3. 建议提交 ROLE_REVIEWER 审查（风控代码强烈建议审查）
```

---

## 你负责的模块

### risk_guardian/ 目录

**已完成**（只读）：
- `circuit_breaker.py` — 熔断触发条件和状态机（核心，理解它再开发其他模块）

**待开发**：

| 文件 | 内容 | 依赖 | 优先级 |
|-----|------|------|-------|
| `exposure_monitor.py` | 实时计算已开仓USD/总资产，超 MAX_EXPOSURE_PCT 告警 | Freqtrade API | P1 |
| `signal_arbiter.py` | AI 信号 vs Freqtrade 内置信号冲突仲裁 | circuit_breaker | P1 |
| `position_sizer.py` | Kelly 公式 + 制度调整系数 | risk.yml | P1 |
| `drawdown_limit.py` | 最大回撤追踪，按日/周/月分级限制 | — | P1 |

### freqtrade_strategies/ 目录

| 文件 | 内容 | 优先级 |
|-----|------|-------|
| `AiSignalStrategy.py` | 从 Redis 读经 risk_guardian 审核的信号，实现 populate_entry_trend | P1 |

---

## 关键模式和约定

### 熔断器状态机（必须理解，不得绕过）

```python
# circuit_breaker.py 已实现，了解它的状态：
# CircuitState.CLOSED  → 正常交易
# CircuitState.OPEN    → 熔断激活，拒绝新开仓

# 触发条件（来自 config/risk.yml）：
# - 单日回撤 >= MAX_DAILY_DRAWDOWN_PCT（默认 5%）
# - 连续亏损 >= MAX_CONSECUTIVE_LOSSES（默认 5 单）
# - 净值低于 EQUITY_FLOOR

# 所有风控模块写订单前必须检查：
from risk_guardian.circuit_breaker import CircuitBreaker
breaker = CircuitBreaker()
if not breaker.is_closed():
    logger.warning("circuit_breaker_open", reason="熔断器激活，拒绝新开仓")
    return  # 直接拒绝，不抛异常
```

### signal_arbiter.py 仲裁规则（固定规则，不得自行发明）

```python
# 规则来自 AI_CONTEXT.md：AI置信度>0.8 且熔断器CLOSED → AI信号优先
def arbitrate(ai_plan: TradePlan, freqtrade_signal: dict) -> dict:
    if not circuit_breaker.is_closed():
        return {"action": "FLAT"}  # 熔断优先于一切
    if ai_plan.confidence > 0.8 and ai_plan.direction != "FLAT":
        return ai_plan_to_order(ai_plan)  # AI 信号优先
    return freqtrade_signal  # 否则用 Freqtrade 内置
```

### position_sizer.py Kelly 公式实现

```python
# Kelly 公式：f = (bp - q) / b
# b = 平均盈亏比，p = 胜率，q = 1 - p
# 制度调整系数来自 ARCH.md 第 7 节
REGIME_MULTIPLIER = {
    "TRENDING": 1.0,
    "RANGING": 0.5,       # 仓位上限降至 40%（原 80% × 0.5）
    "HIGH_VOLATILITY": 0.5,  # 仓位系数 × 0.5
    "UNKNOWN": 0.25,      # 仓位上限 20%
}

def calculate_size(win_rate: float, avg_rr: float, regime: str, equity: float) -> float:
    kelly = (avg_rr * win_rate - (1 - win_rate)) / avg_rr
    kelly = max(0.0, min(kelly, 0.25))  # Kelly 分数上限 25%，防止过激
    multiplier = REGIME_MULTIPLIER.get(regime, 0.25)
    return equity * kelly * multiplier
```

### exposure_monitor.py 对接 Freqtrade

```python
# Freqtrade 提供 REST API，默认 localhost:8080
# 获取持仓：GET /api/v1/status
# 注意：调用时必须处理 Freqtrade 未启动/超时的情况
# 超出 MAX_EXPOSURE_PCT 只告警，不自动平仓（平仓权限归 circuit_breaker）
```

### AiSignalStrategy.py 实现要点

```python
class AiSignalStrategy(IStrategy):
    def populate_entry_trend(self, dataframe, metadata):
        # 从 Redis Stream trade_order 读取信号
        # 注意：只读，不写！写入 trade_order 是 risk_guardian 的权限
        signal = self.read_from_stream("trade_order", metadata["pair"])
        if signal and signal["action"] == "LONG":
            dataframe.loc[..., "enter_long"] = 1
        return dataframe
```

---

## 风控参数（来自 config/risk.yml，不在代码中硬编码）

```yaml
# 这些参数的 key 名必须与 risk.yml 完全一致
MAX_DAILY_DRAWDOWN_PCT: 0.05    # 5%
MAX_CONSECUTIVE_LOSSES: 5
EQUITY_FLOOR: 1000              # USD
MAX_EXPOSURE_PCT: 0.8           # 80% 总资产
CIRCUIT_COOLDOWN_HOURS: 4       # 熔断冷静期
```

---

## 测试要求（风控模块测试优先级最高）

- `test_circuit_breaker.py`（已有）：新增风控功能时必须同步更新
- 每个风控模块需独立测试：
  - `test_signal_arbiter.py`：测试熔断时返回 FLAT，AI 置信度临界值行为
  - `test_position_sizer.py`：测试不同制度的仓位系数，Kelly 上限约束
  - `test_exposure_monitor.py`：mock Freqtrade API，测试超限告警路径
