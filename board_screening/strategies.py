"""筛选策略标识与展示名称。"""

from __future__ import annotations


STRATEGY_EQUAL_DECLINE = "equal_decline"
STRATEGY_MACD_DIVERGENCE = "macd_bottom_divergence"
SUPPORTED_STRATEGIES = (STRATEGY_EQUAL_DECLINE, STRATEGY_MACD_DIVERGENCE)
UNIVERSE_BOARD = "board"
UNIVERSE_STOCK = "stock"
SUPPORTED_UNIVERSES = (UNIVERSE_BOARD, UNIVERSE_STOCK)
STRATEGY_LABELS = {
    STRATEGY_EQUAL_DECLINE: "等距下跌",
    STRATEGY_MACD_DIVERGENCE: "MACD底背离",
}
RUN_MODE_LABELS = {
    (UNIVERSE_BOARD, STRATEGY_EQUAL_DECLINE): "等距下跌",
    (UNIVERSE_BOARD, STRATEGY_MACD_DIVERGENCE): "MACD底背离",
    (UNIVERSE_STOCK, STRATEGY_EQUAL_DECLINE): "个股等距下跌",
    (UNIVERSE_STOCK, STRATEGY_MACD_DIVERGENCE): "个股MACD底背离",
}
SUPPORTED_RUN_MODES = tuple(RUN_MODE_LABELS)


def validate_strategy(strategy: str) -> str:
    if strategy not in SUPPORTED_STRATEGIES:
        raise ValueError(f"不支持的筛选策略：{strategy}")
    return strategy


def validate_universe(universe: str) -> str:
    if universe not in SUPPORTED_UNIVERSES:
        raise ValueError(f"不支持的标的范围：{universe}")
    return universe


def validate_run_mode(strategy: str, universe: str) -> tuple[str, str]:
    validated_strategy = validate_strategy(strategy)
    validated_universe = validate_universe(universe)
    return validated_universe, validated_strategy
