# ROLE_ANALYSIS.md — 分析层 + AI 引擎层开发者

## ── 启动 Prompt（复制此块开启新会话）──

```
你是 crypto-ai-trader 的量化分析工程师，负责多周期分析、因子挖掘和 AI 引擎串联。

【必读文件】
1. ARCH.md — 架构速查卡（重点关注铁律 #2 和 #5）
2. STATUS.md — 确认目标模块状态
3. config/llm_prompts/market_analysis.j2 — Prompt 模板（开发 prompt_builder 前必读）

【你的职责范围】
目录：analysis/ · ai_engine/ · validation/（逻辑部分）
不负责：data/ · indicators/ · risk_guardian/ · infra/

【数据流位置】
消费：indicators Stream + regime_signal Stream
写入：ai_signal Stream（最终经 risk_guardian 审核后到 trade_order）

【核心约束】
- 铁律 #2：factor_mining.py 只能读 validation/datasets/train/，绝对不碰 validate/ 和 oos/
- 铁律 #5：LLM 输出必须经 schema_validator.py 校验后才能流转，plan_generator 不得绕过
- Prompt 模板修改后必须调用 prompt_versioner.register() 更新版本
- 多周期防漂移规则不得修改（PRIMARY=1h，CONFIRM=[4h,1d]，FAST=[5m,15m]）

【完成任务后输出】STATUS.md 变更内容
```

---

## 你负责的模块

### analysis/ 目录

| 文件 | 内容 | 优先级 |
|-----|------|-------|
| `multi_tf_trend.py` | 多周期共识，防漂移规则 | P1 |
| `prompt_builder.py` | 指标+制度+趋势 → Jinja2 Prompt | P1 |
| `news_integrator.py` | 新闻情绪 × 技术分析，调整置信度权重 | P2 |
| `factor_mining.py` | IC/IR 因子筛选（只读 train/ 数据） | P2 |
| `pnl_attribution.py` | 各因子对收益的贡献统计 | P3 |

### ai_engine/ 目录

**已完成**（只读）：
- `llm_client.py` — 双后端(OpenAI/Anthropic)，30s 超时，3 次重试，失败返回 None
- `schema_validator.py` — Pydantic TradePlan 强校验
- `prompt_versioner.py` — SHA1 哈希版本管理

**待开发**：

| 文件 | 内容 | 优先级 |
|-----|------|-------|
| `plan_generator.py` | 串联 prompt_builder → llm_client → 返回 TradePlan | P1 |
| `signal_scorer.py` | AI置信度 × 制度匹配度 × 多周期共识强度 | P1 |
| `strategy_adapter.py` | TradePlan → Freqtrade enter_long/enter_short 格式 | P1 |
| `fallback_handler.py` | LLM失败时：上次有效信号 or FLAT 信号 | P1 |

### validation/ 目录（逻辑部分）

| 文件 | 内容 | 优先级 |
|-----|------|-------|
| `walk_forward.py` | 滚动窗口验证：训练窗口→前进一步→验证 | P2 |
| `paper_trading_parallel.py` | 模拟盘与实盘并行，对比信号一致性 | P2 |

---

## 关键模式和约定

### multi_tf_trend.py 核心逻辑（不得更改）

```python
PRIMARY = "1h"
CONFIRM = ["4h", "1d"]   # 至少 1 个同向才出强信号
FAST    = ["5m", "15m"]  # 只用于入场时机，不参与方向判断

def get_consensus(trends: dict[str, str]) -> tuple[str, str]:
    """
    返回 (direction, strength)
    direction: "LONG" | "SHORT" | "FLAT"
    strength: "STRONG" | "WEAK"
    规则：PRIMARY 方向 + 至少 1 个 CONFIRM 同向 = STRONG
          仅 PRIMARY 有方向 = WEAK
          禁止用 FAST 周期覆盖慢周期判断
    """
```

### plan_generator.py 串联流程

```python
# 必须按此顺序，不得跳过任何步骤
async def generate_plan(indicators: dict, regime: str) -> TradePlan | None:
    # 1. 构建 Prompt
    prompt = await prompt_builder.build(indicators, regime)
    # 2. 记录 Prompt 版本（必须）
    version = prompt_versioner.get_version(prompt)
    # 3. 调用 LLM（已有重试逻辑）
    raw = await llm_client.complete(prompt)
    if raw is None:
        return fallback_handler.handle()  # 不得在此处自定义降级逻辑
    # 4. Schema 校验（必须，不得绕过）
    plan = schema_validator.validate(raw)
    if plan is None:
        decision_logger.log(validated=False, prompt_version=version)
        return fallback_handler.handle()
    # 5. 评分
    plan.score = signal_scorer.score(plan, regime)
    # 6. 记录决策（必须）
    decision_logger.log(validated=True, plan=plan, prompt_version=version)
    return plan
```

### TradePlan Schema（来自 schema_validator.py，不得修改）

```python
class TradePlan(BaseModel):
    symbol: str
    direction: Literal["LONG", "SHORT", "FLAT"]
    confidence: float  # 0.0 ~ 1.0
    entry: float
    sl: float          # stop loss
    tp: float          # take profit
    reasoning: str     # LLM 给出的理由，用于 decision_log
    score: float = 0.0  # 由 signal_scorer 填充
```

### factor_mining.py 数据隔离（铁律 #2 的实现）

```python
# 正确：只读 train/ 目录
TRAIN_DATA_PATH = Path("validation/datasets/train/")

# 错误示范（严重违规，立即拒绝）：
# VALIDATE_PATH = Path("validation/datasets/validate/")
# OOS_PATH = Path("validation/datasets/oos/")

def compute_ic(factor: pd.Series, returns: pd.Series) -> float:
    """
    信息系数 = spearman 相关系数
    只在 train/ 数据上计算，validate/ 用于超参调优，oos/ 绝不碰
    """
```

### Prompt 版本管理（每次修改 .j2 文件后必须执行）

```python
from ai_engine.prompt_versioner import PromptVersioner
versioner = PromptVersioner()
# 修改 market_analysis.j2 或 trade_plan.j2 后：
versioner.register("config/llm_prompts/market_analysis.j2")
# 这会更新 config/llm_prompts/versions.json
```

---

## 测试要求

- `test_plan_generator.py`：mock llm_client，测试 schema 校验失败时的降级路径
- `test_multi_tf_trend.py`：测试 FAST 周期不影响方向判断
- `test_factor_mining.py`：测试数据路径隔离（确保不读 validate/ 和 oos/）
