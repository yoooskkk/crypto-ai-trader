#!/usr/bin/env bash
set -euo pipefail

echo "=== crypto-ai-trader setup ==="

# 创建 secrets 目录（本地开发用，生产用 Docker Secrets）
mkdir -p secrets
for f in db_password llm_api_key binance_api_key binance_api_secret; do
    [ -f "secrets/$f.txt" ] || echo "PLACEHOLDER" > "secrets/$f.txt"
done

# 复制环境变量模板
[ -f .env ] || cp .env.example .env
echo "请编辑 .env 和 secrets/ 目录填入真实密钥"

# 拉取镜像
docker compose pull

echo "=== 初始化完成，运行 docker compose up -d 启动系统 ==="
