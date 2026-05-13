# crypto-ai-trader

基于 Binance 数据 + 多指标体系 + AI引擎 + Freqtrade 的量化交易系统。

## 快速开始
```bash
cp .env.example .env          # 填写 API 密钥
bash scripts/setup.sh         # 初始化环境
docker compose up -d          # 启动所有服务
```

## 系统架构
见 docs/architecture.svg

## 层级说明
| 层级 | 目录 | 职责 |
|------|------|------|
| 数据采集 | data/ | WS+REST+断连重连+数据校验 |
| 消息队列 | messaging/ | Redis Stream 解耦各服务 |
| 指标计算 | indicators/ | 40+ 技术指标 + 币圈因子 |
| 制度识别 | regime/ | HMM 市场状态分类 |
| 分析层   | analysis/ | 多周期趋势 + 因子挖掘 |
| AI 引擎  | ai_engine/ | LLM 交易计划 + Schema 校验 |
| 风险控制 | risk_guardian/ | 熔断 + 暴露度 + 仲裁 |
| 回测验证 | validation/ | Walk-Forward + OOS |
| 策略执行 | freqtrade_strategies/ | Freqtrade 实盘 |
| 可观测性 | observability/ | 决策链路日志 + 告警 |
| 安全层   | security/ | 密钥管理 + 审计 |
