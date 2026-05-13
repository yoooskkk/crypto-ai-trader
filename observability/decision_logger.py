"""
决策链路日志
记录每一次 AI 决策的完整上下文：
输入指标 + Prompt版本 + LLM原始输出 + 校验结果 + 最终信号
存入 TimescaleDB 便于事后复盘
"""
import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DecisionRecord:
    ts:             str
    symbol:         str
    timeframe:      str
    prompt_version: str
    regime:         str
    raw_llm_output: str
    validated:      bool
    direction:      Optional[str]
    confidence:     Optional[float]
    breaker_state:  str
    signal_sent:    bool

class DecisionLogger:
    def __init__(self, db_conn=None):
        self._db = db_conn

    def log(self, record: DecisionRecord) -> None:
        logger.info("DECISION %s", json.dumps(asdict(record)))
        if self._db:
            self._write_db(record)

    def _write_db(self, record: DecisionRecord) -> None:
        # TODO: INSERT INTO decision_log
        pass
