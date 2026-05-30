"""
AI 输出结构化校验
用 Pydantic 强约束 LLM 返回的交易计划格式
"""
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class Direction(str, Enum):
    LONG  = "long"
    SHORT = "short"
    FLAT  = "flat"


class TradePlan(BaseModel):
    symbol:      str
    direction:   Direction
    confidence:  float        = Field(ge=0.0, le=1.0)
    entry_price: Optional[float] = None
    stop_loss:   Optional[float] = None
    take_profit: Optional[float] = None
    reasoning:   str          = Field(min_length=20)
    regime:      str          = "unknown"
    timeframe:   str          = "1h"
    score:       float        = 0.0  # 由 signal_scorer 填充

    @field_validator("stop_loss")
    @classmethod
    def sl_must_be_rational(cls, v, info):
        if v and info.data.get("direction") == Direction.LONG:
            entry = info.data.get("entry_price")
            if entry and v >= entry:
                raise ValueError("LONG stop_loss must be below entry_price")
        return v


def parse_trade_plan(raw: str) -> Optional[TradePlan]:
    import json, re
    try:
        data = json.loads(re.sub(r"```json|```", "", raw).strip())
        return TradePlan(**data)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("Schema validation failed: %s", exc)
        return None
