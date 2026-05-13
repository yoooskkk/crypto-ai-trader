CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS klines (
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    interval    TEXT NOT NULL,
    open        DOUBLE PRECISION,
    high        DOUBLE PRECISION,
    low         DOUBLE PRECISION,
    close       DOUBLE PRECISION,
    volume      DOUBLE PRECISION
);
SELECT create_hypertable('klines', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS indicators (
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    interval    TEXT NOT NULL,
    name        TEXT NOT NULL,
    value       DOUBLE PRECISION
);
SELECT create_hypertable('indicators', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS decision_log (
    ts              TIMESTAMPTZ NOT NULL,
    symbol          TEXT,
    timeframe       TEXT,
    prompt_version  TEXT,
    regime          TEXT,
    validated       BOOLEAN,
    direction       TEXT,
    confidence      DOUBLE PRECISION,
    breaker_state   TEXT,
    signal_sent     BOOLEAN,
    raw_output      TEXT
);
SELECT create_hypertable('decision_log', 'ts', if_not_exists => TRUE);
