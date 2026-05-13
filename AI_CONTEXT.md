# AI_CONTEXT.md — crypto-ai-trader 系统上下文文档

> **使用说明（对 AI）**：这是一份专为 AI 设计的系统上下文文档。
> 当你在新会话中接手此项目时，请先完整阅读本文件，再开始任何代码工作。
> 本文件是整个系统的"单一事实来源"，优先级高于所有其他文档。

---

## 0. 项目一句话定义

`crypto-ai-trader` 是一个基于 **Binance 数据 + 多指标体系 + LLM AI 引擎 + Freqtrade** 的加密货币量化交易系统，
通过 Docker Compose 一键部署，目标是让 AI 自动生成交易计划并通过 Freqtrade 执行。

---

## 1. 系统分层架构（11 层，自上而下）

```
[ 外部数据源 ]  Binance WS/REST · CryptoPanic · Fear&Greed · Twitter
      ↓
[ 数据采集层 ]  data/            WS订阅 · REST补全 · 断连重连 · 数据校验 · 缺口补全
      ↓
[ 消息队列层 ]  messaging/       Redis Stream · 生产者/消费者解耦 · 背压控制
      ↓
[ 指标计算层 ]  indicators/      40+ 技术指标（趋势/动量/波动率/成交量/时间序列/数学/币圈）
      ↓
[ 制度识别层 ]  regime/          HMM + ADX 判断 趋势/震荡/高波动，驱动策略参数切换
      ↓
[ 分析层     ]  analysis/        多周期趋势共识 · 因子挖掘(IC/IR) · Prompt构建 · 新闻融合
      ↓
[ AI 引擎层  ]  ai_engine/       LLM调用 · Schema校验 · 置信度评分 · Prompt版本化 · 降级策略
      ↓
[ 风险控制层 ]  risk_guardian/   熔断器 · 暴露度监控 · 最大回撤限制 · 信号仲裁 · 仓位计算
      ↓
[ 验证层     ]  validation/      Walk-Forward · OOS封存测试 · 因子衰减 · 模拟盘并行
      ↓
[ 策略执行层 ]  freqtrade_strategies/  AiSignalStrategy · 回测 · 实盘执行
      ↓
[ 横切关注点 ]  observability/ · security/  决策链路日志 · 告警 · 密钥管理 · 审计
```

**核心原则**：
- 每层只向下依赖，不向上依赖
- 层间通过 Redis Stream 异步解耦（不做同步调用链）
- `risk_guardian` 是唯一可以 force_exit Freqtrade 仓位的模块

---

## 2. 完整目录结构与每个文件的职责

```
crypto-ai-trader/
│
├── docker-compose.yml          # 7个服务编排：data-collector / indicator-worker /
│                               # regime-worker / ai-engine / risk-guardian /
│                               # freqtrade / infra(redis+timescaledb+influxdb)
├── .env.example                # 所有环境变量模板，复制为 .env 填入真实值
├── requirements.txt            # Python依赖，含版本锁定
├── pyproject.toml              # 项目配置，ruff lint，pytest配置
│
├── data/                       # ═══ 数据采集层 ═══
│   ├── ws_client.py            # 订阅Binance K线/深度/归集成交，断连→reconnect_guard
│   ├── rest_client.py          # 历史K线/OI/资金费率/ticker HTTP拉取
│   ├── reconnect_guard.py      # 指数退避重连（最大60s间隔，20次上限）
│   ├── data_validator.py       # 价格跳空检测(>15%) · 成交量spike(>20x均值) · OHLC逻辑校验
│   ├── gap_filler.py           # WS断连恢复后，用REST补全缺失K线，维护last_ts指针
│   ├── market_selector.py      # 获取Binance前50交易量币种，提供编号/名称输入交互
│   ├── news_scraper.py         # CryptoPanic等新闻源抓取，结构化存入Redis
│   └── sentiment_feed.py       # Fear&Greed指数 + Twitter情绪分数
│
├── messaging/                  # ═══ 消息队列层（服务解耦核心）═══
│   ├── redis_stream.py         # StreamProducer.publish() / StreamConsumer.subscribe()
│   │                           # 消费者组模式，自动ACK，maxlen=10000
│   ├── producer.py             # StreamProducer 便捷导出
│   ├── consumer.py             # StreamConsumer 便捷导出
│   └── backpressure.py         # 队列堆积>5000条时暂停生产者2s
│
│   # Stream 命名约定：
│   # raw_kline       → data层写入，indicator-worker消费
│   # indicators      → indicator-worker写入，regime/ai消费
│   # regime_signal   → regime-worker写入，ai-engine消费
│   # ai_signal       → ai-engine写入，risk-guardian消费
│   # trade_order     → risk-guardian写入，freqtrade消费
│
├── indicators/                 # ═══ 指标计算层 ═══
│   ├── trend.py                # EMA(9/21/55/200) · SMA(20/50/200) · MACD(12,26,9)
│   │                           # ADX(14) · TS_SLOPE
│   ├── momentum.py             # RSI(14) · ROC(10) · CCI(20) · STOCH_K/D(14,3,3)
│   ├── volatility.py           # ATR(14) · STDDEV(20) · BBANDS(20,2)
│   ├── volume.py               # OBV · VWAP · MFI(14) · CMF(20) · VOL_RATIO(20)
│   ├── timeseries.py           # DELAY · DELTA · TS_MAX · TS_MIN · TS_RANK
│   │                           # TS_ZSCORE · CORR
│   ├── math_factors.py         # LOG_RETURN · ZSCORE · RANK · SIGN · ABS_RETURN
│   ├── crypto_alpha.py         # FUNDING_RATE(Binance) · OI_DELTA(24h) · CVD_DELTA(100bar)
│   ├── indicator_display.py    # 格式化输出：指标名 + 当前值 + 含义解释 + 参考意义
│   └── cache_manager.py        # 慢周期(1d/4h/1h)收盘后预计算存Redis，TTL=周期长度
│                               # 快周期(5m/1m)每tick实时计算，不缓存
│
├── regime/                     # ═══ 市场制度识别层 ═══
│   ├── detector.py             # RuleBasedDetector: ADX>25+BB宽度适中→趋势
│   │                           #   ADX<20+BB宽度窄→震荡  BB极宽→高波动
│   ├── hmm_model.py            # HMM 3状态模型，需离线训练后存 models/
│   ├── strategy_switcher.py    # 制度变化时动态修改 config/risk.yml 参数
│   └── models/                 # 训练好的 .pkl 文件（gitignored）
│
│   # Regime 枚举：TRENDING / RANGING / HIGH_VOLATILITY / UNKNOWN
│   # 制度影响：RANGING时关闭趋势信号 · HIGH_VOLATILITY时降低仓位上限50%
│
├── analysis/                   # ═══ 分析层 ═══
│   ├── multi_tf_trend.py       # 多周期共识：1h/4h/1d三周期方向一致才输出强信号
│   │                           # 防漂移：低周期噪声不覆盖高周期判断
│   ├── factor_mining.py        # IC(信息系数)/IR(信息比率)因子筛选
│   │                           # 数据集：train/70% validate/15% oos/15%
│   ├── prompt_builder.py       # 将所有指标+制度+多周期趋势打包成结构化Prompt
│   │                           # 使用 config/llm_prompts/market_analysis.j2 模板
│   ├── news_integrator.py      # 将新闻情绪与技术分析融合，调整置信度权重
│   └── pnl_attribution.py      # 统计各因子对收益的贡献，识别衰减因子
│
├── ai_engine/                  # ═══ AI 引擎层 ═══
│   ├── llm_client.py           # 双后端(OpenAI/Anthropic) · 30s超时 · 3次重试
│   │                           # 失败→返回None→触发fallback_handler
│   ├── schema_validator.py     # Pydantic TradePlan 模型强校验LLM输出
│   │                           # 字段：symbol/direction/confidence/entry/sl/tp/reasoning
│   ├── signal_scorer.py        # 综合评分：AI置信度×制度匹配度×多周期共识强度
│   ├── plan_generator.py       # 调用llm_client，传入prompt_builder输出，返回TradePlan
│   ├── strategy_adapter.py     # TradePlan → Freqtrade enter_long/enter_short 信号格式
│   ├── prompt_versioner.py     # SHA1哈希每个Prompt模板，版本写入versions.json
│   │                           # 每次决策记录使用的版本号，确保可溯源
│   └── fallback_handler.py     # LLM失败时：使用上一次有效信号 or 发出FLAT信号
│
├── risk_guardian/              # ═══ 风险控制层（最高优先级）═══
│   ├── circuit_breaker.py      # 熔断触发条件：
│   │                           #   · 单日回撤 >= MAX_DAILY_DRAWDOWN_PCT (默认5%)
│   │                           #   · 连续亏损 >= MAX_CONSECUTIVE_LOSSES (默认5单)
│   │                           #   · 净值低于 EQUITY_FLOOR
│   │                           # 熔断后：OPEN状态，拒绝所有新开仓，仅允许平仓
│   ├── exposure_monitor.py     # 实时计算 已开仓USD / 总资产，超过MAX_EXPOSURE_PCT告警
│   ├── drawdown_limit.py       # 最大回撤追踪，按日/周/月分级限制
│   ├── signal_arbiter.py       # AI信号 vs Freqtrade内置信号冲突时的仲裁规则
│   │                           # 规则：AI置信度>0.8 且熔断器CLOSED → AI信号优先
│   └── position_sizer.py       # Kelly公式 + 制度调整系数 计算建议仓位
│
│   # 关键约束：risk_guardian 是唯一可以调用 Freqtrade force_exit API 的模块
│   # 任何绕过 risk_guardian 直接写 trade_order stream 的代码都是违规的
│
├── validation/                 # ═══ 回测验证层（防过拟合核心）═══
│   ├── output_schema.py        # BacktestResult / WalkForwardResult Pydantic模型
│   ├── walk_forward.py         # 滚动窗口验证：训练窗口→前进一步→验证
│   ├── oos_test.py             # OOS封存测试集管理，只能在上线前用一次
│   ├── factor_decay.py         # 监控因子IC在时间轴上的衰减，触发阈值→发出告警
│   ├── paper_trading_parallel.py  # 模拟盘与实盘并行，对比信号一致性和绩效偏差
│   └── datasets/
│       ├── train/              # 70% 历史数据 → 用于因子挖掘
│       ├── validate/           # 15% 数据     → Walk-Forward验证
│       └── oos/                # 15% 封存      → 只用一次，用完作废
│
│   # 铁律：factor_mining.py 只能读 train/ 数据，不得碰 validate/ 和 oos/
│
├── freqtrade_strategies/       # ═══ 策略执行层 ═══
│   ├── AiSignalStrategy.py     # 主策略：从Redis读取经risk_guardian审核的信号
│   │                           # populate_entry_trend 读 trade_order stream
│   ├── config.json             # Freqtrade配置：dry_run=true · max_open_trades=5
│   │                           # stake_amount=unlimited · tradable_balance=0.8
│   └── user_data/              # Freqtrade运行数据（data/backtest_results/logs）
│
├── observability/              # ═══ 可观测性（横切关注点）═══
│   ├── decision_logger.py      # 每次AI决策写入 decision_log 超表：
│   │                           # ts/symbol/timeframe/prompt_version/regime/
│   │                           # validated/direction/confidence/breaker_state/signal_sent
│   ├── factor_decay_monitor.py # 定时跑IC计算，写入InfluxDB，Grafana展示衰减曲线
│   ├── alert_manager.py        # 熔断触发/因子衰减/LLM失败率告警（钉钉/TG）
│   └── grafana/dashboards/     # 仪表板JSON配置
│
├── security/                   # ═══ 安全层（横切关注点）═══
│   ├── secrets_loader.py       # 优先级：Docker Secrets > 环境变量 > .env
│   │                           # 永不将密钥写入日志，任何地方
│   ├── audit_logger.py         # 记录：谁/何时/触发了哪个信号/结果
│   └── api_key_rotator.py      # 定期轮换Binance API Key，发送轮换提醒
│
├── infra/
│   ├── redis/redis.conf        # maxmemory=512mb · allkeys-lru · appendonly
│   ├── timescaledb/init.sql    # 超表：klines / indicators / decision_log
│   ├── influxdb/               # 因子IC时序数据
│   └── prometheus/             # Prometheus配置
│
├── config/
│   ├── indicators.yml          # 所有指标的参数（周期/阈值），代码读此文件不硬编码
│   ├── timeframes.yml          # 支持周期列表 + 多周期共识配置
│   ├── risk.yml                # 风控参数（优先级低于环境变量）
│   └── llm_prompts/
│       ├── market_analysis.j2  # 市场分析Prompt Jinja2模板
│       ├── trade_plan.j2       # 交易计划Prompt模板
│       └── versions.json       # Prompt版本注册表（由prompt_versioner维护）
│
├── tests/                      # 单元+集成测试
│   ├── test_circuit_breaker.py # 重点：测试所有熔断触发条件
│   ├── test_schema_validator.py # 重点：测试LLM输出格式校验
│   ├── test_data_validator.py  # 重点：测试价格跳空/成交量异常
│   └── ...
│
└── scripts/
    ├── setup.sh                # 一键初始化：创建secrets目录/拉取镜像
    ├── backfill_data.py        # 历史K线回填到TimescaleDB
    ├── run_backtest.sh         # 触发Freqtrade回测
    └── health_check.py         # 检查所有服务健康状态
```

---

## 3. 数据流全链路（Critical Path）

```
Binance WebSocket
    │ K线推送(每根)
    ▼
data/ws_client.py
    │ 1. data_validator 校验（异常则丢弃+告警）
    │ 2. gap_filler 检测缺口（断连后补全）
    ▼
messaging/producer.py  →  Redis Stream: raw_kline
    │
    ▼ (indicator-worker 消费)
indicators/*.py
    │ 计算40+指标（慢周期读缓存，快周期实时算）
    ▼
Redis Stream: indicators
    │
    ├─▶ (regime-worker 消费)
    │   regime/detector.py  →  判断 TRENDING/RANGING/HIGH_VOLATILITY
    │   Redis Stream: regime_signal
    │
    └─▶ (ai-engine 消费，等待 regime_signal 就绪)
        analysis/multi_tf_trend.py   → 多周期共识
        analysis/prompt_builder.py   → 构建结构化Prompt（含制度/指标/新闻）
        ai_engine/plan_generator.py  → 调用LLM API
        ai_engine/schema_validator.py → 校验TradePlan格式
        ai_engine/signal_scorer.py   → 评分（阈值0.65）
        observability/decision_logger.py → 写 decision_log 超表
        Redis Stream: ai_signal
            │
            ▼ (risk-guardian 消费)
        risk_guardian/circuit_breaker.py  → 熔断检查
        risk_guardian/signal_arbiter.py   → 冲突仲裁
        risk_guardian/position_sizer.py   → 计算仓位
        Redis Stream: trade_order
            │
            ▼
        freqtrade/AiSignalStrategy.py → 执行交易
```

---

## 4. 关键设计决策与约束（开发必读）

### 4.1 不得违反的铁律（Invariants）

| # | 规则 | 原因 |
|---|------|------|
| 1 | `risk_guardian` 是唯一可以写 `trade_order` stream 的模块 | 防止绕过风控直接下单 |
| 2 | `factor_mining.py` 只读 `validation/datasets/train/` | 防止 look-ahead bias / 过拟合 |
| 3 | `validation/datasets/oos/` 只在上线前评估时用一次 | OOS数据一旦"看过"即失效 |
| 4 | 密钥永不出现在日志、代码、Git提交中 | 安全红线 |
| 5 | LLM输出必须经过 `schema_validator.py` 校验才能流转 | 防止LLM幻觉直接触发交易 |
| 6 | 所有指标参数从 `config/indicators.yml` 读取，代码中不硬编码数字 | 可维护性 |
| 7 | 服务间通信只通过 Redis Stream，不做 HTTP 同步调用 | 解耦 + 背压控制 |

### 4.2 市场制度与策略的联动规则

```
TRENDING      → 开启趋势跟随信号 · 正常仓位上限(80%) · MACD/EMA优先
RANGING       → 关闭趋势信号 · 仓位上限降至40% · RSI/STOCH均值回归优先
HIGH_VOLATILITY → 全部信号仓位系数 × 0.5 · 强制收窄止损 · 不开新仓（可选）
UNKNOWN       → 保守模式 · 仓位上限20% · 仅高置信度(>0.8)信号通过
```

### 4.3 多周期防漂移规则

```python
# multi_tf_trend.py 的核心逻辑（不得更改）
# 强信号：主周期 + 确认周期方向一致
# 弱信号：仅主周期有方向
# 禁止：用快周期(5m/1m)覆盖慢周期(4h/1d)的判断

PRIMARY   = "1h"
CONFIRM   = ["4h", "1d"]   # 必须至少1个同向才出强信号
FAST      = ["5m", "15m"]  # 只用于入场时机，不参与方向判断
```

### 4.4 AI Prompt 版本管理规则

- 每次修改 `.j2` 模板文件，必须运行 `prompt_versioner.register()` 更新版本
- `versions.json` 中的版本号会写入每条 `decision_log` 记录
- 回查某笔交易时，可以通过版本号精确还原当时使用的Prompt

### 4.5 Docker Compose 服务依赖顺序

```
redis & timescaledb & influxdb   (基础设施，最先启动)
    ↓
data-collector                   (依赖 redis)
    ↓
indicator-worker                 (依赖 redis + timescaledb)
    ↓
regime-worker                    (依赖 redis)
    ↓
ai-engine                        (依赖 redis + LLM API)
    ↓
risk-guardian                    (依赖 redis + ai-engine)
    ↓
freqtrade                        (依赖 risk-guardian)
```

---

## 5. 技术栈速查

| 类别 | 技术选型 | 用途 |
|------|----------|------|
| 运行时 | Python 3.11+ | 全部服务 |
| 部署 | Docker Compose | 一键启动所有服务 |
| 消息队列 | Redis Stream | 服务间异步解耦 |
| 时序存储 | TimescaleDB(PostgreSQL) | K线原始数据 + 决策日志 |
| 指标存储 | InfluxDB 2.x | 因子IC时序数据 |
| 实时缓存 | Redis | 预计算指标缓存 |
| 指标计算 | pandas-ta + TA-Lib | 技术指标 |
| 制度识别 | hmmlearn + scikit-learn | HMM市场状态分类 |
| AI后端 | OpenAI GPT-4o / Anthropic Claude | LLM交易计划生成 |
| 数据校验 | Pydantic v2 | LLM输出Schema校验 |
| Prompt模板 | Jinja2 | 结构化Prompt渲染 |
| 策略执行 | Freqtrade stable | 回测/实盘/仓位管理 |
| 可观测性 | Prometheus + Grafana | 指标监控 |
| 结构化日志 | structlog | 全服务日志 |

---

## 6. 当前开发状态（迭代时更新此节）

> AI 接手新任务前，必须检查此节了解已完成/进行中/待开发的模块状态。

### 已完成（代码骨架已生成，可直接开发）
- [x] 整体目录结构（由 `setup_project.py` 生成）
- [x] `data/ws_client.py` — 基本框架
- [x] `data/reconnect_guard.py` — 完整实现
- [x] `data/data_validator.py` — 完整实现
- [x] `data/gap_filler.py` — 完整实现
- [x] `messaging/redis_stream.py` — 完整实现
- [x] `messaging/backpressure.py` — 完整实现
- [x] `regime/detector.py` — 规则方法完整实现
- [x] `ai_engine/llm_client.py` — 完整实现
- [x] `ai_engine/schema_validator.py` — 完整实现
- [x] `ai_engine/prompt_versioner.py` — 完整实现
- [x] `risk_guardian/circuit_breaker.py` — 完整实现
- [x] `validation/output_schema.py` — 完整实现
- [x] `security/secrets_loader.py` — 完整实现
- [x] `observability/decision_logger.py` — 框架完整
- [x] `infra/timescaledb/init.sql` — 3张超表建表语句
- [x] `docker-compose.yml` — 7服务完整编排
- [x] `config/indicators.yml` — 所有指标参数
- [x] `config/risk.yml` — 风控参数
- [x] `config/llm_prompts/market_analysis.j2` — Prompt模板
- [x] `indicators/trend.py` — 所有趋势指标计算实现完毕

### 待开发（Stub文件，内部逻辑需实现）
- [ ] `indicators/momentum.py` — 需实现动量指标
- [ ] `indicators/volatility.py` — 需实现波动率指标
- [ ] `indicators/volume.py` — 需实现成交量指标
- [ ] `indicators/timeseries.py` — 需实现时间序列因子
- [ ] `indicators/crypto_alpha.py` — 需对接Binance资金费率/OI API
- [ ] `indicators/cache_manager.py` — 需实现慢周期预计算缓存
- [ ] `analysis/multi_tf_trend.py` — 需实现多周期共识逻辑
- [ ] `analysis/factor_mining.py` — 需实现IC/IR计算
- [ ] `analysis/prompt_builder.py` — 需实现Jinja2模板渲染
- [ ] `analysis/news_integrator.py` — 需实现情绪权重融合
- [ ] `regime/hmm_model.py` — 需实现HMM训练和推理
- [ ] `regime/strategy_switcher.py` — 需实现参数动态切换
- [ ] `ai_engine/signal_scorer.py` — 需实现综合评分
- [ ] `ai_engine/plan_generator.py` — 需串联prompt_builder+llm_client
- [ ] `ai_engine/strategy_adapter.py` — 需实现TradePlan→Freqtrade格式转换
- [ ] `risk_guardian/exposure_monitor.py` — 需对接Freqtrade API
- [ ] `risk_guardian/signal_arbiter.py` — 需实现仲裁规则
- [ ] `risk_guardian/position_sizer.py` — 需实现Kelly公式
- [ ] `validation/walk_forward.py` — 需实现滚动窗口框架
- [ ] `freqtrade_strategies/AiSignalStrategy.py` — 需实现Redis信号读取
- [ ] `ui/cli/coin_selector.py` — 需实现前50列表+交互
- [ ] `ui/cli/timeframe_picker.py` — 需实现周期选择
- [ ] `ui/cli/indicator_panel.py` — 需实现美化展示

### 已知问题 / 技术债
- [ ] `regime/hmm_model.py` 需要离线训练数据，训练脚本待写
- [ ] 新闻情绪历史数据难以获取，回测时需要mock
- [ ] Freqtrade force_exit API的调用方式需要验证版本兼容性

---

## 7. 开发规范（AI编写代码必须遵守）

### 代码风格
- Python 3.11+，使用 `async/await`，所有IO操作异步化
- 类型注解必填，`from __future__ import annotations` 开头
- 行宽100字符（pyproject.toml已配置ruff）
- 日志用 `structlog`，不用 `print`
- 配置从环境变量或 `config/*.yml` 读取，**不硬编码任何数字或密钥**

### 新增模块规范
```python
# 每个新模块的标准头部注释格式
"""
模块名称
所属层级: [数据采集/消息队列/指标计算/制度识别/分析/AI引擎/风险控制/验证/执行]
输入来源: [说明从哪个Stream或调用哪个模块获取数据]
输出去向: [说明向哪个Stream写入或返回什么]
关键依赖: [列出import的内部模块]
"""
```

### 修改现有模块规范
1. 先读懂当前文件的完整逻辑，再修改
2. 不得改变函数签名（除非同步更新所有调用方）
3. 不得修改 Stream 名称（会破坏消费者）
4. 风控相关代码修改必须同步更新 `tests/test_circuit_breaker.py`

### Git提交规范
```
feat(layer): 简短描述    # 新功能
fix(layer): 简短描述     # 修复
refactor(layer): 描述    # 重构
test(layer): 描述        # 测试

# layer 示例: data / messaging / indicators / regime /
#             analysis / ai_engine / risk_guardian / validation
```

---

## 8. 快速上手指引（新AI接手任务时的标准流程）

```
Step 1: 阅读本文件（AI_CONTEXT.md）全文
Step 2: 查看第6节"当前开发状态"，确认要开发的模块状态
Step 3: 阅读目标模块的现有代码（即使是stub也要读注释）
Step 4: 确认输入来源（读哪个Stream/调哪个函数）
Step 5: 确认输出去向（写哪个Stream/返回什么格式）
Step 6: 确认是否涉及 risk_guardian（若是，必须走铁律#1）
Step 7: 编写代码，遵守第7节规范
Step 8: 更新本文件第6节的开发状态
```

---

## 9. 常见问题 Q&A

**Q: 我想修改某个指标的计算参数，在哪里改？**
A: 只改 `config/indicators.yml`，代码从该文件读取，不要动 `.py` 文件中的数字。

**Q: 我想新增一个指标，步骤是什么？**
A: 1) 在对应的 `indicators/xxx.py` 里新增函数 2) 在 `indicators.yml` 中添加参数 3) 在 `indicator_display.py` 中添加展示说明 4) 确认 `prompt_builder.py` 会包含此指标。

**Q: LLM返回的格式不对怎么办？**
A: `schema_validator.py` 会返回 `None`，触发 `fallback_handler.py` 使用上一次有效信号，同时 `decision_logger` 记录 `validated=False`，`observability/alert_manager.py` 发出告警。

**Q: 熔断器触发了，如何恢复？**
A: 1) 查看 `decision_log` 找到触发原因 2) 确认风险已解除 3) 调用 `CircuitBreaker.reset()` 4) 系统自动恢复正常交易。默认冷静期4小时（`config/risk.yml`中配置）。

**Q: 想新增一个时间周期，怎么做？**
A: 在 `config/timeframes.yml` 的 `available` 列表中添加，`indicators/cache_manager.py` 和 `analysis/multi_tf_trend.py` 会自动读取配置，无需改代码。

**Q: 如何安全地添加新的外部数据源？**
A: 1) 在 `data/` 下新建采集文件 2) 用 `StreamProducer` 写入新的Stream名 3) 在 `messaging/redis_stream.py` 注册Stream名常量 4) 在 `docker-compose.yml` 确认网络权限 5) 密钥通过 `security/secrets_loader.py` 加载。

---

*最后更新：系统架构设计阶段完成，进入模块实现阶段*
*本文件由开发者/AI协作维护，每次迭代后更新第6节*





这是 crypto-ai-trader 的核心上下文文档。请你扮演资深量化架构师，深度解析第 3 节的数据链路和第 4 节的铁律。现在，根据第 6 节的‘待开发’清单，我们先实现 indicators/trend.py。请严格遵守 config/indicators.yml 的配置读取规范。