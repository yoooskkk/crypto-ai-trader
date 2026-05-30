# STATUS.md — 当前开发状态（唯一需要频繁更新的文件）

> 每个 AI 角色完成任务后，输出此文件的变更内容，由人类手动合并。
> 不在这里的模块 = 尚未排期。

---

## 已完成模块

| 模块文件 | 完成程度 | 备注 |
|---------|---------|------|
| `data/ws_client.py` | 框架完成 | 基本框架，可继续完善 |
| `data/reconnect_guard.py` | ✅ 完整 | |
| `data/data_validator.py` | ✅ 完整 | |
| `data/gap_filler.py` | ✅ 完整 | |
| `messaging/redis_stream.py` | ✅ 完整 | |
| `messaging/backpressure.py` | ✅ 完整 | |
| `regime/detector.py` | ✅ 完整（规则方法）| HMM 方法尚未实现 |
| `ai_engine/llm_client.py` | ✅ 完整 | |
| `ai_engine/schema_validator.py` | ✅ 完整 | |
| `ai_engine/prompt_versioner.py` | ✅ 完整 | |
| `risk_guardian/circuit_breaker.py` | ✅ 完整 | |
| `validation/output_schema.py` | ✅ 完整 | |
| `security/secrets_loader.py` | ✅ 完整 | |
| `observability/decision_logger.py` | 框架完成 | 写入逻辑待完善 |
| `infra/timescaledb/init.sql` | ✅ 完整 | 3 张超表 |
| `docker-compose.yml` | ✅ 完整 | 7 服务 |
| `config/indicators.yml` | ✅ 完整 | |
| `config/risk.yml` | ✅ 完整 | |
| `config/llm_prompts/market_analysis.j2` | ✅ 完整 | |
| `indicators/trend.py` | ✅ 完整 | |
| `indicators/momentum.py` | ✅ 完整 (v2.0) | 重写为 DataFrame→DataFrame 模式，RSI+ROC+CCI+STOCH |
| `indicators/volatility.py` | ✅ 完整 | ATR+STDDEV+BBANDS |
| `indicators/volume.py` | ✅ 完整 | OBV+VWAP+MFI+CMF+VOL_RATIO |
| `indicators/timeseries.py` | ✅ 完整 | DELAY+DELTA+TS_MAX/MIN/RANK/ZSCORE+CORR |
| `indicators/cache_manager.py` | ✅ 完整 | 慢周期缓存 + TTL 管理，序列化/反序列化 |
| `regime/strategy_switcher.py` | ✅ 完整 | 制度→风险参数映射表，带备份的 risk.yml 动态更新 |
| `regime/strategy_switcher.py` | ✅ 完整 | 制度→风险参数映射，带备份的 risk.yml 动态更新 |
| `indicators/crypto_alpha.py` | ✅ 完整 | FUNDING_RATE(Binance API)、OI_DELTA(24h)、CVD_DELTA(100bar) |
| `regime/hmm_model.py` | ✅ 完整 | 5 维特征（自实现，无 pandas_ta 依赖）+ GaussianHMM 训练/推理 + 自动制度映射 + RuleBased 降级 |
---

## 待开发模块

### 优先级 P1（数据流核心路径）

| 模块文件 | 负责角色 | 状态 |
|---------|---------|------|
| `analysis/multi_tf_trend.py` | ROLE_ANALYSIS | stub 存在 |
| `analysis/prompt_builder.py` | ROLE_ANALYSIS | stub 存在 |
| `ai_engine/plan_generator.py` | ROLE_ANALYSIS | stub 存在 |
| `ai_engine/signal_scorer.py` | ROLE_ANALYSIS | stub 存在 |
| `ai_engine/strategy_adapter.py` | ROLE_ANALYSIS | stub 存在 |
| `risk_guardian/exposure_monitor.py` | ROLE_RISK | stub 存在 |
| `risk_guardian/signal_arbiter.py` | ROLE_RISK | stub 存在 |
| `risk_guardian/position_sizer.py` | ROLE_RISK | stub 存在 |
| `freqtrade_strategies/AiSignalStrategy.py` | ROLE_RISK | stub 存在 |

### 优先级 P2（增强功能）

| 模块文件 | 负责角色 | 状态 |
|---------|---------|------|
| `analysis/factor_mining.py` | ROLE_ANALYSIS | 需实现 IC/IR |
| `analysis/news_integrator.py` | ROLE_ANALYSIS | 情绪权重融合 |
| `validation/walk_forward.py` | ROLE_ANALYSIS | 滚动窗口框架 |

### 优先级 P3（CLI 界面）

| 模块文件 | 负责角色 | 状态 |
|---------|---------|------|
| `ui/cli/coin_selector.py` | ROLE_DATA | 待创建 |
| `ui/cli/timeframe_picker.py` | ROLE_DATA | 待创建 |
| `ui/cli/indicator_panel.py` | ROLE_INDICATORS | 待创建 |

---

## 已知问题 / 技术债

| 问题 | 严重程度 | 负责角色 |
|-----|---------|---------|
| `regime/hmm_model.py` 需要离线训练数据，训练脚本未写 | 中 | ROLE_INDICATORS |
| 新闻情绪历史数据难以获取，回测时需要 mock | 低 | ROLE_ANALYSIS |
| Freqtrade force_exit API 调用方式需验证版本兼容性 | 高 | ROLE_RISK |
| detector.py 的 Regime 枚举值小写 "trending" vs STREAM_SCHEMA.md 大写 "TRENDING" | 低 | ROLE_INDICATORS |
| config/indicators.yml 缺少 timeseries 段，timeseries.py 使用默认参数运行并记录 warning | 低 | ROLE_INDICATORS |
| indicators/trend.py 仍使用 logging 而非 structlog（与 ARCH.md 规范不一致） | 低 | ROLE_REVIEWER |
| indicators/math_factors.py 为 TODO 但不在 ROLE_INDICATORS.md 清单中，需确认保留或删除 | 低 | 人类决策 |
| crypto_alpha.py 依赖 aiohttp 调用 Binance Futures API，需在生产环境配置代理或白名单 | 中 | ROLE_INFRA |
| regime/hmm_model.py 缓存路径 data/historical/ 需在 .gitignore 中添加 | 低 | ROLE_INFRA |
| regime/hmm_model.py 训练需 aiohttp，目前为延迟导入（lazy import） | 低 | ROLE_INDICATORS |
---

## 更新记录

| 日期 | 更新内容 | 更新者 |
|-----|---------|-------|
| 初始化 | 系统架构设计阶段完成，进入模块实现阶段 | 人类 |
| 2025-05-30 | indicators/ P1 全部 5 个模块 + regime/strategy_switcher.py 实现完成 | ROLE_INDICATORS |
| 2025-05-30 | regime/strategy_switcher.py + indicators/crypto_alpha.py 完成 | ROLE_INDICATORS |
| 2025-05-30 | regime/hmm_model.py 完成，P2 全部结束 | ROLE_INDICATORS |
---


*此文件每次迭代后由完成任务的 AI 角色输出 diff，人类合并*
