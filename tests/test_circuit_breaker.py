"""
CircuitBreaker 测试（已迁移至 test_risk_guardian.py）。
运行：python -m pytest tests/test_risk_guardian.py -v -k TestCircuitBreaker
"""
from __future__ import annotations

from tests.test_risk_guardian import TestCircuitBreakerBase, TestCircuitBreakerIntegration


class TestCircuitBreaker(TestCircuitBreakerBase):
    """CircuitBreaker 基础测试（兼容旧引用）。"""
    pass


class TestCircuitBreakerIntegrationAlias(TestCircuitBreakerIntegration):
    """CircuitBreaker 集成测试（兼容旧引用）。"""
    pass

