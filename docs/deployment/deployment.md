# crypto-ai-trader 部署文档

> 从零到生产环境的完整部署指南。预计阅读时间：15 分钟。
> 目标：10 分钟内完成首次 `docker compose up -d`。

---

## 目录

1. [系统架构总览](#1-系统架构总览)
2. [前置条件](#2-前置条件)
3. [快速开始（3 分钟）](#3-快速开始-3-分钟)
4. [生产部署](#4-生产部署)
   - [4.1 环境变量配置](#41-环境变量配置)
   - [4.2 密钥管理](#42-密钥管理)
   - [4.3 Docker 部署](#43-docker-部署)
   - [4.4 验证部署](#44-验证部署)
5. [服务详解](#5-服务详解)
6. [配置参考](#6-配置参考)
7. [升级指南](#7-升级指南)
8. [回滚指南](#8-回滚指南)

---

## 1. 系统架构总览

```
┌─────────────────────────────────────────────────────────┐
│                     external network                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐   │
│  │ Binance  │  │ Crypto-  │  │     Freqtrade         │   │
│  │ WebSocket│  │ Panic    │  │  (策略执行引擎)       │   │
│  └────┬─────┘  └──────────┘  └──────────┬───────────┘   │
│       │                                  │               │
├───────┼──────────────────────────────────┼───────────────┤
│       │           internal network        │               │
│  ┌────▼───────────────────────────────────▼────┐         │
│  │           data-collector (ws_client)         │         │
│  │           ┌── Redis Stream ──┐              │         │
│  │  raw_kline│                  │indicators    │         │
│  ├───────────┴──► indicator-worker ◄───────────┤         │
│  │              ┌── Redis Stream ──┐           │         │
│  │  indicators  │                  │regime_sig │         │
│  ├──────────────┴──► regime-worker ◄───────────┤         │
│  │              ┌── Redis Stream ──┐           │         │
│  │  regime_sig  │                  │ai_signal  │         │
│  ├──────────────┴──► ai-engine ◄───────────────┤         │
│  │              ┌── Redis Stream ──┐           │         │
│  │  ai_signal   │                  │trade_order│         │
│  ├──────────────┴──► risk-guardian ◄───────────┤         │
│  │                         │                   │         │
│  │                    force_exit API           │         │
│  │                         │                   │         │
│  │              ┌──────────▼────────┐          │         │
│  │              │    Freqtrade      │          │         │
│  │              │    (docker)       │          │         │
│  └──────────────┴───────────────────┴──────────┘         │
│                                                           │
│  ┌── 基础服务 ────────────────────────────────────────┐  │
│  │  Redis  │  TimescaleDB  │  InfluxDB  │  Prometheus  │  │
│  │  Grafana│  AlertManager │  Dashboard │  HealthCheck │  │
│  └──────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────┘
```

### 数据流

| 步骤 | Stream | 生产者 | 消费者 | 说明 |
|:----:|:-------|:-------|:-------|:-----|
| 1 | `raw_kline` | data-collector | indicator-worker | Binance WS → K 线 |
| 2 | `indicators` | indicator-worker | regime-worker | 60+ 技术指标 |
| 3 | `regime_signal` | regime-worker | ai-engine | 市场制度（趋势/震荡/高波动） |
| 4 | `ai_signal` | ai-engine | risk-guardian | AI 生成的交易信号 |
| 5 | `trade_order` | risk-guardian | Freqtrade | 最终交易指令 |

### 容器清单（共 12 个）

| 容器 | 依赖 | 端口 | 说明 |
|:-----|:-----|:----:|:-----|
| **redis** | — | 6379 | 消息队列骨干 |
| **timescaledb** | — | 5432 | 决策日志持久化 |
| **influxdb** | — | 8086 | 因子衰减时序数据 |
| **prometheus** | — | 9090 | 指标采集 |
| **grafana** | prometheus | 3000 | 可视化面板 |
| **alertmanager** | prometheus | 9093 | 告警路由 |
| **dashboard** | timescaledb, redis | 8080 | Web 仪表板 |
| **data-collector** | redis | — | Binance WS 数据采集 |
| **indicator-worker** | redis, timescaledb | — | 指标计算 |
| **regime-worker** | redis, timescaledb | — | 制度识别 |
| **ai-engine** | redis | — | LLM 交易计划生成 |
| **risk-guardian** | redis | — | 风控审核 |
| **freqtrade** | risk-guardian | 8080 | 策略执行引擎（可选） |

---

## 2. 前置条件

| 依赖 | 版本要求 | 验证命令 |
|:-----|:---------|:---------|
| Docker | ≥ 24.0 | `docker --version` |
| Docker Compose | ≥ 2.24 | `docker compose version` |
| Git | ≥ 2.40 | `git --version` |
| 内存 | ≥ 8 GB | `free -h` |
| 磁盘 | ≥ 20 GB | `df -h` |
| 网络 | 可访问 api.binance.com | `curl -s https://api.binance.com/api/v3/ping` |

### 检查清单

- [ ] Docker 已安装且当前用户有权限
- [ ] `git clone` 已完成
- [ ] 如有 Freqtrade，已准备 `config.json` 和策略文件
- [ ] 已创建 `./secrets/` 目录和密钥文件（见 4.2 节）

---

## 3. 快速开始（3 分钟）

> 适用于本地开发测试。生产部署见第 4 节。

```shell
# 1. 克隆仓库
git clone <repo-url> crypto-ai-trader
cd crypto-ai-trader

# 2. 创建密钥文件（测试用，生产见 4.2）
mkdir -p secrets
echo "sk-your-test-key" > secrets/llm_api_key.txt
echo "your-binance-api-key" > secrets/binance_api_key.txt
echo "your-binance-api-secret" > secrets/binance_api_secret.txt
echo "trader" > secrets/db_password.txt

# 3. 创建 .env（可选，不创建则全部使用默认值）
# 注意：.env 不在版本控制中
# cp docs/deployment/.env.example .env

# 4. 启动全部服务
docker compose up -d

# 5. 确认全部容器健康
docker compose ps

# 6. 查看日志
docker compose logs -f
```

首次启动时，Docker 会构建镜像（约 2-5 分钟）。后续启动为秒级。

### 验证

```shell
# 健康检查（各服务状态）
docker compose exec health-check python -m scripts.health_check --json

# 检查 Redis Stream 是否正常运行
docker compose exec redis redis-cli ping
# 预期输出: PONG

# 检查 Web 面板
curl http://localhost:8080/health
# 预期输出: {"status": "ok", ...}
```

---

## 4. 生产部署

### 4.1 环境变量配置

系统使用以下优先级加载配置（高优先级覆盖低优先级）：

```
构造函数参数 > 环境变量 > YAML 配置文件 > 代码默认值
```

所有服务共享同一份 `.env` 文件，由 `docker compose` 通过 `env_file: .env` 注入。

**关键环境变量速查表**：

| 变量名 | 必需 | 默认值 | 说明 |
|:-------|:----:|:-------|:-----|
| `LOG_LEVEL` | 否 | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_JSON` | 否 | `false` | 生产推荐设为 `true`（JSON 格式，便于日志聚合） |
| `SYMBOLS` | 否 | `BTCUSDT,ETHUSDT` | 监控的交易对，逗号分隔 |
| `KLINE_INTERVAL` | 否 | `1m` | K 线周期：`1m`/`5m`/`15m`/`1h`/`4h`/`1d` |
| `MAX_DAILY_DRAWDOWN_PCT` | 否 | `5.0` | 日最大回撤阈值（%） |
| `MAX_CONSECUTIVE_LOSSES` | 否 | `5` | 连续亏损次数触发熔断 |
| `FREQTRADE_PASSWORD` | 见说明 | — | 如启用 Freqtrade 则为必需 |
| `ALERT_TELEGRAM_BOT_TOKEN` | 否 | — | Telegram 告警通知 |

完整列表见 [`docs/deployment/.env.example`](.env.example)。

### 4.2 密钥管理

> **铁律**：密钥**永不**出现在代码、日志或 `.env` 文件中。所有密钥通过 Docker Secrets 挂载。

创建 `./secrets/` 目录（已在 `.gitignore` 中排除）：

```shell
mkdir -p secrets
```

**必需密钥文件**：

| 文件 | 格式 | 示例 | 用途 |
|:----|:-----|:-----|:-----|
| `secrets/llm_api_key.txt` | 一行 API Key | `sk-proj-xxxxxxxx` | OpenAI / Anthropic LLM 调用 |
| `secrets/binance_api_key.txt` | 一行 API Key | `xxxxxxxxxxxx` | Binance REST API (仅 public endpoints) |
| `secrets/binance_api_secret.txt` | 一行 Secret | `xxxxxxxxxxxx` | Binance REST API (仅 public endpoints) |
| `secrets/db_password.txt` | 一行密码 | `your_strong_password` | TimescaleDB 数据库密码 |

> **安全提示**：
> - 生产环境务必使用强密码（≥ 20 字符）
> - `db_password.txt` 的密码必须与 `.env` 中的 `TIMESCALEDB_PASSWORD` 一致
> - 定期轮换 API Key（建议每 90 天）
> - 确保 `secrets/` 目录权限为 `chmod 700 secrets/`

**LLM API Key 多提供商支持**：

`secrets/llm_api_key.txt` 可包含多行，每行格式 `provider=key`：

```
openai=sk-proj-xxxxx
anthropic=sk-ant-xxxxx
```

单行时默认用于 OpenAI。

### 4.3 Docker 部署

#### 4.3.1 完整部署（生产）

```shell
# 1. 准备密钥文件（见 4.2 节）
# 2. 准备 .env 文件
# 3. 构建并启动
docker compose build --no-cache    # 首次构建
docker compose up -d               # 启动全部服务

# 4. 等待初始化完成（约 30 秒）
sleep 30
docker compose ps
```

#### 4.3.2 部分部署（仅数据采集 + 指标）

```shell
docker compose up -d redis timescaledb \
  data-collector indicator-worker
```

#### 4.3.3 仅启动风控（已有外部数据源）

```shell
docker compose up -d redis \
  regime-worker ai-engine risk-guardian
```

### 4.4 验证部署

#### 4.4.1 快速检查

```shell
# 容器状态
docker compose ps

# 各服务健康检查
docker compose exec health-check python -m scripts.health_check \
  --service redis --service timescaledb --json
```

#### 4.4.2 数据流验证

```shell
# 查看 Redis Stream 长度（确认数据流动）
docker compose exec redis redis-cli XLEN raw_kline
docker compose exec redis redis-cli XLEN indicators
docker compose exec redis redis-cli XLEN trade_order

# 查看最新消息
docker compose exec redis redis-cli XREVRANGE raw_kline + - COUNT 1
```

#### 4.4.3 日志查看

```shell
# 全部日志（推荐 JSON 格式时用 jq 过滤）
docker compose logs -f | grep "指标计算完成\|制度识别完成\|AI 信号生成完成\|风控审核通过"

# 单服务日志
docker compose logs -f indicator-worker
docker compose logs -f ai-engine

# 错误日志
docker compose logs | grep -i "error\|exception\|failed"
```

#### 4.4.4 面板访问

| 面板 | URL | 默认凭据 |
|:-----|:----|:---------|
| Web Dashboard | `http://localhost:8080` | 无认证 |
| Grafana | `http://localhost:3000` | `admin` / `admin` |
| Prometheus | `http://localhost:9090` | 无认证 |
| AlertManager | `http://localhost:9093` | 无认证 |

---

## 5. 服务详解

### 5.1 data-collector

```shell
# 调整监控币种和周期
SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT    # 在 .env 中设置
KLINE_INTERVAL=1h                   # Binance WebSocket 周期
```

采集的 K 线数据通过 `raw_kline` Redis Stream 发布。不依赖数据库。

### 5.2 indicator-worker

从 `raw_kline` Stream 消费，维护滑动窗口缓存（最多 300 根 K 线），计算 60+ 技术指标。

**预热时间**：每种 (symbol, timeframe) 组合需要至少 200 根 K 线才能开始产出指标。

### 5.3 regime-worker

消费指标数据，使用 ADX + Bollinger Band 宽度规则识别市场制度：

| 条件 | 制度 |
|:-----|:-----|
| BB 宽度 > 0.08 | `HIGH_VOLATILITY` |
| ADX > 25 | `TRENDING` |
| ADX < 20 且 BB 宽度 < 0.02 | `RANGING` |
| 其他 | `UNKNOWN` |

同时支持 HMM 模型（需离线训练），见 `scripts/train_hmm.py`。

### 5.4 ai-engine

核心模块：接收制度信号 → 调用 PlanGenerator（含 LLM）→ 生成交易计划 → 发布 ai_signal。

**重要**：此服务需要访问外部 LLM API（OpenAI / Anthropic），需确保：
- `secrets/llm_api_key.txt` 文件存在且有效
- 容器可访问外部网络（`external` 网络已连接）

### 5.5 risk-guardian

风控审核链：熔断器 → 回撤检查 → 仓位计算 → 信号仲裁。

如需与 Freqtrade 集成（熔断时强平），需配置：

```yaml
# .env
FREQTRADE_API_URL=http://freqtrade:8080
FREQTRADE_PASSWORD=your_freqtrade_password
```

### 5.6 Dashboard

基于 FastAPI + Jinja2 的 Web 面板，提供：
- `/api/health` — 系统健康状态
- `/api/signals` — 最近交易信号
- `/api/risk` — 风控状态
- `/api/factors` — 因子衰减
- `/api/status` — 全系统状态总览

---

## 6. 配置参考

### 6.1 配置文件清单

| 文件 | 格式 | 说明 |
|:-----|:-----|:-----|
| `config/indicators.yml` | YAML | 指标参数（EMA 周期、RSI 周期等） |
| `config/risk.yml` | YAML | 风控参数（熔断阈值、回撤限制、仓位限制） |
| `config/llm_prompts/*.j2` | Jinja2 | LLM 提示词模板 |
| `freqtrade_strategies/config.json` | JSON | Freqtrade 策略配置（需手动创建） |
| `infra/prometheus/prometheus.yml` | YAML | Prometheus 抓取目标 |
| `infra/alertmanager/config.yml` | YAML | 告警路由配置 |

### 6.2 动态配置（热更新）

以下配置支持运行时修改，**无需重启容器**：

- `config/risk.yml` — 由 `regime-worker` 在制度切换时自动更新（修改会备份为 `.bak`）
- `config/indicators.yml` — 指标参数（下次消费新 K 线时生效）

### 6.3 HMM 模型

通过 `scripts/train_hmm.py` 训练的模型存储在：

```
data/historical/hmm_models/{symbol}_{timeframe}.pkl
```

模型升级只需用 `--force-refresh` 重新训练：

```shell
docker compose exec regime-worker python -m scripts.train_hmm \
  --symbol BTCUSDT --timeframe 1h --force-refresh
```

---

## 7. 升级指南

### 7.1 常规升级（无 schema 变更）

```shell
# 1. 拉取最新代码
git pull origin main

# 2. 重新构建并启动
docker compose build --no-cache
docker compose up -d

# 3. 验证
docker compose ps
```

### 7.2 含数据库 schema 变更

```shell
# 1. 备份数据库
docker compose exec timescaledb pg_dump -U trader crypto_trader > backup_$(date +%Y%m%d).sql

# 2. 更新代码并重启
git pull origin main
docker compose build --no-cache
docker compose up -d

# 3. 确认数据完整
docker compose exec health-check python -m scripts.health_check --json
```

### 7.3 LLM 提示词更新

提示词模板在 `config/llm_prompts/` 目录下。修改后**无需重启**：

```shell
# 修改模板
vim config/llm_prompts/market_analysis.j2

# 触发版本注册（下次 AI 信号生成时自动注册新版本）
# 新版本 SHA 会记录在 ai_signal 的 prompt_version 字段中
```

---

## 8. 回滚指南

### 8.1 Docker 回滚

```shell
# 1. 回退到上一版本
docker compose down
git checkout HEAD~1
docker compose build --no-cache
docker compose up -d

# 2. 或使用特定版本标签（需提前打 tag）
git checkout tags/v1.0.0
docker compose up -d --build
```

### 8.2 数据库回滚

```shell
# 1. 停止使用数据库的服务
docker compose stop dashboard indicator-worker regime-worker

# 2. 恢复备份
cat backup_20250101.sql | docker compose exec -T timescaledb \
  psql -U trader crypto_trader

# 3. 重启
docker compose up -d
```

### 8.3 HMM 模型回滚

HMM 模型文件按时间戳命名保留多个版本：

```shell
ls -la data/historical/hmm_models/
# BTCUSDT_1h_20250101T000000.pkl
# BTCUSDT_1h_20250108T000000.pkl  ← 回滚到此

# 手动复制旧版本即可
cp data/historical/hmm_models/BTCUSDT_1h_20250101T000000.pkl \
   data/historical/hmm_models/BTCUSDT_1h.pkl
```

---

## 附录

### A. 端口占用检查

如果端口冲突，修改 `docker-compose.yml` 中的 `ports:` 映射：

```yaml
services:
  redis:
    ports:
      - "127.0.0.1:16379:6379"   # 将宿主机 6379 改为 16379
```

### B. 资源限制建议

| 服务 | CPU | 内存 | 磁盘 |
|:-----|:---:|:----:|:----:|
| Redis | 0.5 | 512 MB | — |
| TimescaleDB | 1.0 | 1 GB | 10 GB+ |
| indicator-worker | 1.0 | 512 MB | — |
| ai-engine | 1.0 | 1 GB | — |
| Freqtrade | 0.5 | 256 MB | — |

在 `docker-compose.yml` 中设置：

```yaml
services:
  timescaledb:
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 1G
```

### C. 安全实践

1. **网络隔离**：`internal` 网络禁止外部访问，只有 `data-collector` 和 `freqtrade` 连接 `external` 网络
2. **端口绑定**：所有端口绑定到 `127.0.0.1`，不暴露到公网
3. **非 root 运行**：Docker 容器以 `trader` 用户（uid 1000）运行
4. **健康检查**：每个容器有 HEALTHCHECK，Docker 自动重启不健康容器
5. **密钥轮换**：建议每 90 天轮换一次 API Key 和数据库密码

### D. 相关文档

| 文档 | 说明 |
|:-----|:-----|
| `docs/context/ARCH.md` | 系统架构设计 |
| `docs/context/STATUS.md` | 当前开发状态 |
| `docs/contracts/STREAM_SCHEMA.md` | Redis Stream 消息格式 |
| `docs/deployment/operations.md` | 运维手册 |
| `docs/deployment/troubleshooting.md` | 常见问题排查 |
