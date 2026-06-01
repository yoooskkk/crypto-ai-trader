# STATUS.md — 当前开发状态（唯一需要频繁更新的文件）

> 每个 AI 角色完成任务后，输出此文件的变更内容，由人类手动合并。
> 不在这里的模块 = 尚未排期。

---

## 已完成模块

| 模块文件 | 完成程度 | 备注 |
|---------|---------|------|
| `data/ws_client.py` | ✅ 完整 | 已完善 structlog + K 线字段标准化 |
| `data/reconnect_guard.py` | ✅ 完整 | |
| `data/data_validator.py` | ✅ 完整 | |
| `data/gap_filler.py` | ✅ 完整 | |
| `data/rest_client.py` | ✅ 完整 | Binance 现货+合约 REST 封装，24hr Ticker + 排名 |
| `data/market_selector.py` | ✅ 完整 | MarketSelector 交互式+编程式币种选择 |
| `data/news_scraper.py` | ✅ 完整 | CryptoPanic 新闻抓取 + Redis 存储 |
| `data/sentiment_feed.py` | ✅ 完整 | Fear & Greed 指数获取 + Redis 存储 |
| `tests/test_rest_client.py` | ✅ 完整 | 24 个测试覆盖 K 线/Ticker/错误/重试/资源管理 |
| `messaging/redis_stream.py` | ✅ 完整 | |
| `messaging/backpressure.py` | ✅ 完整 | |
| `regime/detector.py` | ✅ 完整（规则方法）| HMM 方法尚未实现 |
| `ai_engine/llm_client.py` | ✅ 完整 | |
| `ai_engine/schema_validator.py` | ✅ 完整 | |
| `ai_engine/prompt_versioner.py` | ✅ 完整 | |
| `risk_guardian/circuit_breaker.py` | ✅ 完整 | 含熔断器与仲裁器集成测试 |
| `risk_guardian/drawdown_limit.py` | ✅ 完整 | 日/周/月分级回撤追踪 + 从 risk.yml 加载 |
| `risk_guardian/exposure_monitor.py` | ✅ 完整 | 多持仓汇总 + API 异常优雅处理 |
| `risk_guardian/position_sizer.py` | ✅ 完整 | Kelly 公式 + 制度乘数 + 最小仓位阈值 |
| `risk_guardian/signal_arbiter.py` | ✅ 完整 | 仲裁规则 + audit_id + Stream 消息输出 |
| `freqtrade_strategies/AiSignalStrategy.py` | ✅ 完整 | load_signal_from_payload 无需 freqtrade 可独立导入 |
| `tests/test_risk_guardian.py` | ✅ 完整 | 54 测试覆盖 6 个模块（全部通过）|
| `validation/output_schema.py` | ✅ 完整 | |
| `security/secrets_loader.py` | ✅ 完整 | |
| `observability/decision_logger.py` | ✅ 完整 |  asyncpg 写入 TimescaleDB + 连接池管理 + 降级日志 + 查询接口，19 测试全部通过 |
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
| `indicators/crypto_alpha.py` | ✅ 完整 | FUNDING_RATE(Binance API)、OI_DELTA(24h)、CVD_DELTA(100bar) |
| `regime/hmm_model.py` | ✅ 完整 | 5 维特征（自实现，无 pandas_ta 依赖）+ GaussianHMM 训练/推理 + 自动制度映射 + RuleBased 降级 |
| `indicators/math_factors.py` | ✅ 完整 | LOG_RETURN+ZSCORE+RANK+SIGN+ABS_RETURN，纯 pandas/numpy 实现 |
| `analysis/multi_tf_trend.py` | ✅ 完整 | 多周期共识 + 防漂移规则，21 测试通过 |
| `analysis/prompt_builder.py` | ✅ 完整 | Jinja2 模板渲染 + 指标/制度注入 |
| `analysis/factor_mining.py` | ✅ 完整 | IC/IR 计算 + Spearman 排序 + 铁律 #2 隔离，17 测试通过 |
| `analysis/news_integrator.py` | ✅ 完整 | 双通道情绪融合（新闻+F&G），11 测试通过 |
| `analysis/pnl_attribution.py` | ✅ 完整 | 多维度归因分析（夏普/索提诺/回撤/因子相关性），11 测试通过 |
| `ai_engine/plan_generator.py` | ✅ 完整 | 6 步串联 + Schema 校验 + 降级路径，10 测试通过 |
| `ai_engine/signal_scorer.py` | ✅ 完整 | 三维评分（AI 置信度 × 制度匹配 × 多周期共识）|
| `ai_engine/strategy_adapter.py` | ✅ 完整 | TradePlan → Freqtrade 信号 + ai_signal Stream |
| `ai_engine/fallback_handler.py` | ✅ 完整 | 两级降级（复用上次信号 → FLAT 安全信号）|
| `tests/test_multi_tf_trend.py` | ✅ 完整 | 21 测试，覆盖方向推断/共识/FAST 防漂移 |
| `tests/test_plan_generator.py` | ✅ 完整 | 10 测试，覆盖正常/降级/FLAT/信号格式/状态缓存 |
| `tests/test_factor_mining.py` | ✅ 完整 | 17 测试，覆盖铁律 #2/IC/IR/异常处理 |
| `tests/test_news_integrator.py` | ✅ 完整 | 11 测试，覆盖权重融合/极端覆写/数据缺省 |
| `tests/test_pnl_attribution.py` | ✅ 完整 | 11 测试，覆盖空/单/多交易/分组统计/因子相关性 |
| `validation/walk_forward.py` | ✅ 完整 |  32 测试，实现滚动窗口验证引擎（含简化回测 + 夏普/盈亏比/回撤指标）|
| `validation/factor_decay.py` | ✅ 完整 |  因子 IC 衰减监控：均值/斜率/半衰期分析 + scipy 线性回归 |
| `validation/oos_test.py` | ✅ 完整 |  铁律 #3 OOS 单次使用 + .oos_used 标记文件保护 |
| `validation/paper_trading_parallel.py` | ✅ 完整 |  回测/模拟盘信号对比：方向一致率·置信度相关·频次比 |
| `tests/test_validation.py` | ✅ 完整 |  21 测试覆盖 3 个新模块 |
| `ui/cli/coin_selector.py` | ✅ 完整 |  交互式币种选择器，依赖 MarketSelector |
| `ui/cli/timeframe_picker.py` | ✅ 完整 |  交互式单/多周期选择器 |
| `ui/cli/indicator_panel.py` | ✅ 完整 |  交互式指标选择面板（6 类别切换/指标开关/参数预览/配置导出），21 测试全部通过 |
---

## 待开发模块

### 优先级 P1（数据流核心路径）

_全部 P1 模块已完成 ✅_

### 优先级 P2（增强功能）

| 模块文件 | 负责角色 | 状态 |
|---------|---------|------|


### 优先级 P3（CLI 界面）

_全部 P3 模块已完成 ✅_

---

## 已知问题 / 技术债

| 问题 | 严重程度 | 负责角色 |
|-----|---------|---------|
| `regime/hmm_model.py` 需要离线训练数据，训练脚本未写 | 中 | ROLE_INDICATORS |
| 新闻情绪历史数据难以获取，回测时需要 mock | 低 | ROLE_ANALYSIS |
| Freqtrade force_exit API 调用方式需验证版本兼容性 | 高 | ROLE_RISK |
| crypto_alpha.py 依赖 aiohttp 调用 Binance Futures API，需在生产环境配置代理或白名单 | 中 | ROLE_INFRA |

**已解决 ✅**
| 问题 | 解决方案 |
|-----|---------|
| detector.py 枚举值大小写不一致 | `Regime` 值改为大写（TRENDING/RANGING/HIGH_VOLATILITY/UNKNOWN）|
| config/indicators.yml 缺少 timeseries 段 | 已添加完整配置段 |
| 6 个文件使用 logging 而非 structlog | trend.py / reconnect_guard.py / gap_filler.py / circuit_breaker.py / llm_client.py / prompt_versioner.py 已迁移 |
| data/historical/ 缓存路径未加入 .gitignore | 已添加 |

---

## 更新记录

| 日期 | 更新内容 | 更新者 |
|-----|---------|-------|
| 初始化 | 系统架构设计阶段完成，进入模块实现阶段 | 人类 |
| 2025-05-30 | indicators/ P1 全部 5 个模块 + regime/strategy_switcher.py 实现完成 | ROLE_INDICATORS |
| 2025-05-30 | regime/strategy_switcher.py + indicators/crypto_alpha.py 完成 | ROLE_INDICATORS |
| 2025-05-30 | regime/hmm_model.py 完成，P2 全部结束 | ROLE_INDICATORS |
| 2025-05-30 | indicators/math_factors.py 完成（原 TODO stub），已加入 config/indicators.yml 配置段 | ROLE_INDICATORS |
| 2025-05-30 | data/rest_client.py / market_selector.py / news_scraper.py / sentiment_feed.py 完成 | ROLE_DATA |
| 2025-05-30 | ui/cli/coin_selector.py / timeframe_picker.py 完成 | ROLE_DATA |
| 2025-05-30 | data/ws_client.py 完善（structlog + Binance K 线字段映射）| ROLE_DATA |
| 2025-05-30 | tests/test_rest_client.py 完成（24 测试全部通过）| ROLE_DATA |
| 2025-05-30 | analysis/ P1（multi_tf_trend + prompt_builder）+ ai_engine/ P1 全部 4 模块（plan_generator/signal_scorer/strategy_adapter/fallback_handler）完成，55 测试全部通过，Prompt 版本已注册（market_analysis=f0086a27, trade_plan=d91dabb4）| ROLE_ANALYSIS |
| 2025-05-30 | analysis/ P2（factor_mining + news_integrator）+ P3（pnl_attribution）完成，39 测试全部通过 | ROLE_ANALYSIS |
| 2025-06-01 | risk_guardian/ P1 全部 5 模块（circuit_breaker/drawdown_limit/exposure_monitor/position_sizer/signal_arbiter）+ freqtrade_strategies/AiSignalStrategy + test_risk_guardian.py 完成，54 测试全部通过 ✅ | ROLE_RISK |
| 2025-06-01 | validation/walk_forward.py 实现滚动窗口验证引擎（含简化回测 + 夏普/盈亏比/回撤指标），32 测试全部通过 ✅ | ROLE_ANALYSIS |
| 2025-06-01 | validation/factor_decay.py（因子衰减监控）/ oos_test.py（铁律 #3 单次使用）/ paper_trading_parallel.py（信号对比）+ test_validation.py 完成，53 测试全部通过 ✅ | ROLE_ANALYSIS |
| 2025-06-01 | ui/cli/indicator_panel.py 交互式指标选择面板（类别切换/指标开关/参数查看/配置导出），21 测试全部通过 ✅ | ROLE_INDICATORS |
| 2025-06-01 | observability/decision_logger.py 完善写入逻辑（asyncpg 连接池 + INSERT/查询 + 降级） + plan_generator await 修复，19+10 测试全部通过 ✅ | ROLE_ANALYSIS |
| 2025-06-01 | 技术债清理一批：indicators.yml 加 timeseries 段 · detector.py 枚举值大写统一 · .gitignore 加 data/historical/ | ROLE_INDICATORS |
| 2025-06-01 | 技术债清理二批：6 个文件 logging → structlog 迁移（trend / reconnect_guard / gap_filler / circuit_breaker / llm_client / prompt_versioner）| ROLE_REVIEWER |
---


*此文件每次迭代后由完成任务的 AI 角色输出 diff，人类合并*
