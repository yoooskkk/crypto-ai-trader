"""
回测验证 Schema — 与 ai_engine/schema_validator.py 共享 TradePlan 模型
另外定义回测结果的验证结构
"""
from pydantic import BaseModel, Field


class BacktestResult(BaseModel):
    strategy:       str
    start_date:     str
    end_date:       str
    total_trades:   int   = Field(ge=0)
    win_rate:       float = Field(ge=0.0, le=1.0)
    sharpe:         float
    max_drawdown:   float = Field(le=0.0)
    profit_factor:  float = Field(ge=0.0)
    avg_trade_pct:  float


class WalkForwardResult(BaseModel):
    windows: list[BacktestResult]
    avg_sharpe:      float
    sharpe_variance: float
    robust:          bool  # sharpe_variance < threshold
