"""筛选策略标识与展示名称。"""

from __future__ import annotations


STRATEGY_EQUAL_DECLINE = "equal_decline"
STRATEGY_MACD_DIVERGENCE = "macd_bottom_divergence"
SUPPORTED_STRATEGIES = (STRATEGY_EQUAL_DECLINE, STRATEGY_MACD_DIVERGENCE)
STRATEGY_LABELS = {
    STRATEGY_EQUAL_DECLINE: "等距下跌",
    STRATEGY_MACD_DIVERGENCE: "MACD底背离",
}


def validate_strategy(strategy: str) -> str:
    if strategy not in SUPPORTED_STRATEGIES:
        raise ValueError(f"不支持的筛选策略：{strategy}")
    return strategy
