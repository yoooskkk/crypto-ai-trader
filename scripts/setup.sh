#!/usr/bin/env bash
set -euo pipefail

echo "=== crypto-ai-trader setup ==="

# Python 环境
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo "Error: Python 3 not found"
    exit 1
fi

echo "[1/5] Creating Python virtual environment..."
if [ ! -d .venv ]; then
    $PY -m venv .venv
fi
source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate 2>/dev/null || true

echo "[2/5] Installing dependencies..."
pip install -q --upgrade pip
if [ -f requirements.txt ]; then
    pip install -q -r requirements.txt
fi
if [ -f requirements-dev.txt ]; then
    pip install -q -r requirements-dev.txt 2>/dev/null || true
fi

echo "[3/5] Creating secrets directory..."
mkdir -p secrets
for f in db_password llm_api_key binance_api_key binance_api_secret; do
    fp="secrets/$f.txt"
    if [ ! -f "$fp" ]; then
        echo "PLACEHOLDER_CHANGE_ME" > "$fp"
        echo "  (Created $fp - please fill with real credentials)"
    fi
done

echo "[4/5] Creating .env file..."
if [ ! -f .env ]; then
    cat > .env << 'ENVEOF'
SYMBOLS=BTCUSDT,ETHUSDT
TIMEFRAMES=1h,4h,1d
TIMESCALEDB_HOST=localhost
TIMESCALEDB_PORT=5432
TIMESCALEDB_USER=trader
TIMESCALEDB_PASSWORD=trader
TIMESCALEDB_DB=crypto_trader
INFLUXDB_URL=http://localhost:8086
INFLUXDB_TOKEN=
INFLUXDB_ORG=crypto_trader
INFLUXDB_BUCKET=factor_decay
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8080
FREQTRADE_API_URL=http://localhost:8080
FREQTRADE_USERNAME=Freqtrader
FREQTRADE_PASSWORD=
MAX_DAILY_DRAWDOWN_PCT=5.0
MAX_CONSECUTIVE_LOSSES=5
MAX_TOTAL_EXPOSURE_PCT=100
EQUITY_FLOOR_USD=0
ENVEOF
    echo ".env created with defaults"
fi

echo "[5/5] Verifying project structure..."
$PY -c "
import importlib
for m in ['risk_guardian','indicators','analysis','validation','observability','ui','scripts','config']:
    try:
        importlib.import_module(m)
        print(f'  [OK] {m}')
    except ImportError:
        print(f'  [--] {m}')
"

echo ""
echo "=== Setup complete ==="
echo "Run: docker compose up -d"
echo "Or: source .venv/bin/activate  (dev mode)"
echo "Edit secrets/*.txt and .env with real credentials"