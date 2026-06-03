# =============================================================================
# crypto-ai-trader — 多阶段构建
# =============================================================================
# 基础镜像: Python 3.14 slim
# 构建产物: /app 目录下的可执行包
# 运行时: 非 root 用户 (uid 1000)
# =============================================================================

# ─── Stage 1: 依赖安装 ──────────────────────────────────────────────────
FROM python:3.14-slim AS builder

WORKDIR /build

# 系统依赖（编译某些 wheel 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖声明
COPY requirements.txt requirements-dev.txt ./

# 安装依赖到 /install 目录（仅使用预编译 wheel，不编译源码）
# 注: 所有指标模块已改用纯 pandas/numpy 实现，无需 pandas_ta/ta-lib/numba
RUN pip install --only-binary :all: --no-cache-dir --prefix=/install \
    -r requirements.txt \
    -r requirements-dev.txt || \
    pip install --only-binary :all: --no-cache-dir --prefix=/install -r requirements.txt

# ─── Stage 2: 运行时 ────────────────────────────────────────────────────
FROM python:3.14-slim AS runtime

# 系统依赖（运行时需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 复制依赖
COPY --from=builder /install /usr/local

# 创建非 root 用户
RUN groupadd -g 1000 trader && \
    useradd -m -u 1000 -g trader -s /bin/bash trader

WORKDIR /app

# 复制项目代码
COPY --chown=trader:trader . .

# 创建必要目录
RUN mkdir -p /app/logs /app/secrets && \
    chown -R trader:trader /app/logs /app/secrets

USER trader

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -m scripts.health_check --service self --json || exit 1

# 默认命令（可由 docker-compose 覆盖）
CMD ["python", "-m", "scripts.cli_main", "check-env"]

# ─── 元数据 ─────────────────────────────────────────────────────────────
LABEL org.opencontainers.image.title="crypto-ai-trader" \
      org.opencontainers.image.description="AI 驱动的加密货币量化交易系统" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.authors="crypto-ai-trader team"
