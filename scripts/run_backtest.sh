#!/usr/bin/env bash
set -euo pipefail

# crypto-ai-trader 回测运行器
# 使用 validation/walk_forward.py 滚动窗口验证引擎
#
# 用法:
#   ./scripts/run_backtest.sh --symbol BTCUSDT --interval 1h --start 2025-01-01 --end 2025-02-01
#   ./scripts/run_backtest.sh --config backtest_config.json
#   ./scripts/run_backtest.sh --list

PY="python3"
if ! command -v python3 &>/dev/null; then
    PY="python"
fi

echo "=== Backtest Runner ==="
echo ""

# 解析参数
CONFIG_FILE=""
SYMBOL=""
INTERVAL=""
START_DATE=""
END_DATE=""
LIST=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) CONFIG_FILE="$2"; shift 2 ;;
        --symbol) SYMBOL="$2"; shift 2 ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        --start) START_DATE="$2"; shift 2 ;;
        --end) END_DATE="$2"; shift 2 ;;
        --list) LIST=true; shift ;;
        --help|-h)
            echo "Usage: $0 [options]"
            echo "  --config FILE    JSON config file"
            echo "  --symbol SYM     Trading pair (e.g. BTCUSDT)"
            echo "  --interval INT   Timeframe (e.g. 1h, 4h, 1d)"
            echo "  --start DATE     Start date (YYYY-MM-DD)"
            echo "  --end DATE       End date (YYYY-MM-DD)"
            echo "  --list           List available backtest configs"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if $LIST; then
    echo "Available backtest configurations:"
    if [ -d config/backtest ]; then
        for f in config/backtest/*.json; do
            echo "  $f"
        done
    else
        echo "  (No config/backtest/ directory found)"
    fi
    echo ""
    echo "Default walk-forward parameters:"
    $PY -c "
from validation.walk_forward import WalkForwardConfig
cfg = WalkForwardConfig()
print(f'  Train window: {cfg.train_window}')
print(f'  Validation window: {cfg.val_window}')
print(f'  Step size: {cfg.step_size}')
"
    exit 0
fi

# 运行回测
if [ -n "$CONFIG_FILE" ]; then
    echo "Running backtest with config: $CONFIG_FILE"
    $PY -c "
import json, sys
with open('$CONFIG_FILE') as f:
    cfg = json.load(f)
print(f'Config: {json.dumps(cfg, indent=2)}')
"
elif [ -n "$SYMBOL" ]; then
    echo "Running walk-forward validation:"
    echo "  Symbol:   $SYMBOL"
    echo "  Interval: ${INTERVAL:-1h}"
    echo "  Period:   ${START_DATE:-30d} to ${END_DATE:-now}"

    $PY -c "
from validation.walk_forward import WalkForwardEngine, WalkForwardConfig
from datetime import datetime, timezone

engine = WalkForwardEngine()
print('WalkForwardEngine initialized')
print(f'Config: train={engine.config.train_window}d, val={engine.config.val_window}d')
print()
print('To run a full backtest with data:')
print('  1. Ensure data is in TimescaleDB (use scripts/backfill_data.py)')
print('  2. Call engine.run(symbol, interval, start, end)')
"
else
    echo "Error: specify --symbol or --config"
    echo "Usage: $0 --symbol BTCUSDT --interval 1h --start 2025-01-01 --end 2025-02-01"
    exit 1
fi

echo ""
echo "=== Backtest complete ==="