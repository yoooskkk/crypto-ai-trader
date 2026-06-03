# crypto-ai-trader 常见问题排查

> 按症状分类的故障排查手册。遇到问题先搜索此文档。

---

## 目录

1. [启动失败](#1-启动失败)
2. [数据流中断](#2-数据流中断)
3. [服务异常](#3-服务异常)
4. [性能问题](#4-性能问题)
5. [常见错误信息](#5-常见错误信息)

---

## 1. 启动失败

### 1.1 Docker 构建失败

**症状**：`docker compose build` 中途报错退出。

**排查步骤**：

```shell
# 查看详细构建日志
docker compose build --no-cache 2>&1 | tail -50

# 常见原因
```

| 错误 | 原因 | 解决 |
|:-----|:-----|:-----|
| `Could not find a version that satisfies the requirement ta-lib` | TA-Lib 需要系统编译依赖 | 确保 Dockerfile 中的 `gcc` 安装成功 |
| `Cannot connect to the Docker daemon` | Docker 未运行 | `systemctl start docker` |
| `Build failed: unknown instruction` | Dockerfile 语法错误 | 检查是否有特殊字符 |
| `ERROR: failed to solve: failed to read dockerfile` | 当前目录不对 | `cd crypto-ai-trader` |

### 1.2 容器启动后立即退出

**症状**：`docker compose ps` 显示容器 `Exit` 状态。

```shell
# 查看退出容器的日志
docker compose logs <container-name>

# 常见原因
```

| 容器 | 日志关键字 | 解决 |
|:-----|:-----------|:-----|
| data-collector | `Symbol list is empty` | 设置 `SYMBOLS` 环境变量 |
| ai-engine | `API key not found` | 检查 `secrets/llm_api_key.txt` |
| risk-guardian | `FREQTRADE_PASSWORD` | Freqtrade 未启用时忽略此警告 |
| timescaledb | `password authentication failed` | 检查 `secrets/db_password.txt` 与 `.env` 一致 |
| health-check | `Connection refused` | 依赖的服务尚未就绪，等待 30 秒后重试 |

### 1.3 端口冲突

**症状**：`Error starting userland proxy: listen tcp 127.0.0.1:6379: bind: address already in use`

```shell
# 查找占用端口的进程
netstat -ano | findstr :6379

# 修改端口映射
vim docker-compose.yml
# 将 6379:6379 改为 16379:6379
```

### 1.4 secrets 目录缺失

**症状**：`services.xxx.secrets - secret db_password is not defined`

```shell
# 修复
mkdir -p secrets
echo "trader" > secrets/db_password.txt
echo "sk-your-key" > secrets/llm_api_key.txt
echo "your-api-key" > secrets/binance_api_key.txt
echo "your-api-secret" > secrets/binance_api_secret.txt
```

---

## 2. 数据流中断

### 2.1 Redis Stream 空（无数据流动）

**症状**：所有 Stream 长度为 0。

```shell
# 验证 Redis 正常运行
docker compose exec redis redis-cli PING

# 检查 data-collector 日志
docker compose logs --tail=30 data-collector
```

**可能原因**：
1. **Binance WS 连接失败**：检查网络 `curl https://api.binance.com/api/v3/ping`
2. **API Key 无效**：检查 `secrets/binance_api_key.txt`
3. **WebSocket 重连循环**：日志中出现 `重连中...` 字样

**解决方案**：

```shell
# 重启数据采集
docker compose restart data-collector

# 检查是否需要代理（部分网络环境需要）
# 在 .env 中设置 HTTP_PROXY / HTTPS_PROXY
```

### 2.2 raw_kline 有数据，但 indicators 为空

**症状**：`XLEN raw_kline` > 0 但 `XLEN indicators` = 0。

```shell
# 查看指标 worker 日志
docker compose logs --tail=30 indicator-worker
```

**可能原因**：
1. **缓存预热中**（最常见）— 需要至少 200 根 K 线
   - 等待即可
   - 或使用 `scripts/backfill_data.py` 回填历史数据
2. **K 线格式异常** — 检查 data-collector 输出的 raw_kline 格式

### 2.3 indicators 有数据，但 regime_signal 为空

**症状**：`XLEN indicators` > 0 但 `XLEN regime_signal` = 0。

```shell
# 查看 regime worker 日志
docker compose logs --tail=30 regime-worker
```

**可能原因**：
1. `indicators` 消息中缺少 `indicators` 字段或为空
2. 制度检测器初始化失败

**解决方案**：

```shell
# 重启制度 worker
docker compose restart regime-worker
```

### 2.4 regime_signal 有数据，但 ai_signal 为空

**症状**：数据流在 AI 引擎中断。

```shell
# 查看 AI 引擎日志
docker compose logs --tail=30 ai-engine
```

**常见原因**：

| 日志 | 原因 | 解决 |
|:-----|:-----|:-----|
| `无指标数据可用，跳过 AI 信号生成` | AI 引擎需要 close 价格 | regime_signal 消息必须包含 `close` 字段 |
| `交易计划生成异常` | LLM API 调用失败 | 检查 API Key 和网络 |
| `LLM API 不可达` | 网络问题 | 检查代理配置 |
| `计划为空（降级/FALLBACK）` | LLM 返回了空计划 | 正常行为，系统会自动降级 |

### 2.5 ai_signal 有数据，但 trade_order 为空

**症状**：风控阶段数据中断。

```shell
# 查看风控日志
docker compose logs --tail=30 risk-guardian
```

**可能原因**：
1. 熔断器已打开（`状态: OPEN`）
2. 所有信号被仲裁器拒绝（信号质量不够）
3. 风控模块初始化失败

**解决方案**：

```shell
# 查看熔断器状态
docker compose exec risk-guardian python -c "
from risk_guardian.circuit_breaker import CircuitBreaker
cb = CircuitBreaker()
print('State:', cb.state)
print('Consecutive losses:', cb._consec)
"

# 如果需要手动重置（仅在确认风险解除后）
docker compose restart risk-guardian
```

---

## 3. 服务异常

### 3.1 Prometheus 抓取不到指标

**症状**：Grafana 面板显示无数据。

```shell
# 检查 Prometheus 目标状态
curl http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | {job, health, lastScrape, scrapeDuration}'

# 检查各服务 /metrics 端点
curl http://localhost:8001/metrics 2>/dev/null || echo "ai-engine metrics not reachable"
curl http://localhost:8002/metrics 2>/dev/null || echo "risk-guardian metrics not reachable"
```

**常见原因**：

| 症状 | 原因 | 解决 |
|:-----|:-----|:-----|
| `context deadline exceeded` | 网络延迟 | 检查 Prometheus 与目标之间网络 |
| `connection refused` | 服务未启动 | 确认目标容器运行中 |
| `target down` | metrics 端口未暴露 | 检查 Prometheus exporter 配置 |

### 3.2 Grafana 面板空白

**症状**：Grafana 能访问但面板无图表。

```shell
# 检查 Grafana 数据源配置
# 1. 登录 http://localhost:3000
# 2. 进入 Configuration → Data Sources → Prometheus
# 3. URL 应为 http://prometheus:9090

# 重试：
docker compose restart grafana
```

### 3.3 TimescaleDB 连接池耗尽

**症状**：`FATAL: remaining connection slots are reserved for non-replication superuser connections`

```shell
# 当前连接数
docker compose exec timescaledb psql -U trader crypto_trader -c \
  "SELECT count(*) FROM pg_stat_activity;"

# 修改最大连接数
docker compose exec timescaledb psql -U trader crypto_trader -c \
  "ALTER SYSTEM SET max_connections = 200;"
docker compose restart timescaledb
```

### 3.4 Freqtrade 连接失败

**症状**：`Freqtrade API 连接失败` 或 `Freqtrade API 认证失败`

```shell
# 检查 Freqtrade 运行状态
docker compose ps freqtrade

# 测试 API
curl http://localhost:8080/api/v1/ping

# 常见原因
```

| 错误 | 原因 | 解决 |
|:-----|:-----|:-----|
| `Connection refused` | Freqtrade 未启动 | `docker compose up -d freqtrade` |
| `401 Unauthorized` | JWT 登录失败 | 检查 `FREQTRADE_PASSWORD` |
| `timeout` | 网络延迟 | 增大 `FREQTRADE_API_TIMEOUT`（默认 10s） |

---

## 4. 性能问题

### 4.1 CPU 过高

**可能原因**：
1. **indicator-worker** 缓存过大 — 减少 `_CACHE_SIZE`（默认 300）
2. **数据采集** WebSocket 连接数过多 — 减少 `SYMBOLS` 数量
3. **HMM 训练** 并发过高 — 使用 `--concurrency 1` 限流

### 4.2 内存泄漏

**排查**：

```shell
# 查看各容器内存使用
docker stats --no-stream

# 检查 indicator-worker 缓存大小
docker compose exec indicator-worker python -c "
from indicators.processor import _kline_cache
print(f'缓存条目数: {sum(len(v) for v in _kline_cache.values())}')
"
```

**常见原因**：
1. `_CACHE_SIZE` 过大（每个 (symbol, timeframe) 对缓存过多）
2. Redis Stream 堆积（背压未生效）
3. `_latest_indicators` 缓存未清理

### 4.3 磁盘空间不足

```shell
# 检查 Docker 磁盘使用
docker system df

# 清理未使用的镜像、容器和卷
docker system prune -f

# 清理特定数据
docker compose exec timescaledb psql -U trader crypto_trader -c \
  "SELECT pg_size_pretty(pg_database_size('crypto_trader'));"
```

---

## 5. 常见错误信息

### 5.1 `Redis Stream 堆积超过阈值`

**级别**：WARNING

**可能原因**：下游处理速度跟不上上游生产速度。

**解决**：
1. 增加对应 worker 的消费者数（当前每个 Stream 只有一个消费者）
2. 检查下游是否有性能瓶颈
3. 检查 `messaging/backpressure.py` 中的 `MAX_PENDING`

### 5.2 `API key not found`

**级别**：ERROR

**可能原因**：`secrets/llm_api_key.txt` 不存在或为空。

**解决**：创建密钥文件并重启：

```shell
echo "sk-proj-xxxxxxxx" > secrets/llm_api_key.txt
docker compose restart ai-engine
```

### 5.3 `Symbol list is empty`

**级别**：ERROR

**可能原因**：未设置 `SYMBOLS` 环境变量。

**解决**：在 `.env` 中添加：

```shell
SYMBOLS=BTCUSDT,ETHUSDT
```

### 5.4 `password authentication failed`

**级别**：ERROR

**可能原因**：`secrets/db_password.txt` 中的密码与 `TIMESCALEDB_PASSWORD` 不一致。

**解决**：

```shell
# 确保两者一致
cat secrets/db_password.txt
# 输出: your_password

grep TIMESCALEDB_PASSWORD .env
# 输出: TIMESCALEDB_PASSWORD=your_password
```

### 5.5 `Market closed` / `No kline for current period`

**级别**：INFO

**含义**：当前非交易时段。加密货币市场 24/7 运行，通常不会出现此消息。如果出现，可能是数据源问题。

---

## 附录

### A. 容器重置命令速查

| 操作 | 命令 |
|:-----|:-----|
| 重启所有 | `docker compose restart` |
| 重启单个 | `docker compose restart <service>` |
| 重建单个 | `docker compose build <service> && docker compose up -d <service>` |
| 查看日志 | `docker compose logs -f <service>` |
| 进入容器 | `docker compose exec <service> sh` |
| 停止所有 | `docker compose stop` |
| 停止并删除 | `docker compose down` |
| 清理数据卷 | `docker compose down -v` （⚠️ 危险） |

### B. 诊断命令速查

| 诊断项 | 命令 |
|:-------|:-----|
| 容器状态 | `docker compose ps` |
| 资源使用 | `docker stats --no-stream` |
| Redis Ping | `docker compose exec redis redis-cli PING` |
| Redis Stream 长度 | `docker compose exec redis redis-cli XLEN <stream>` |
| 数据库连接 | `docker compose exec timescaledb psql -U trader -c "SELECT 1;"` |
| HH M模型列表 | `python -m scripts.train_hmm --list-models` |
| 健康检查 | `python -m scripts.health_check --json` |
| 环境变量 | `python -m scripts.cli_main check-env` |
