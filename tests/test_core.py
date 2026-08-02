from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from board_screening.core import (
    OUTPUT_COLUMNS,
    calculate_decline_target_prices,
    calculate_post_target_drawdown,
    calculate_signed_target_deviation,
    is_target_price_qualified,
)


def test_target_price_allows_exactly_three_percent_above() -> None:
    assert is_target_price_qualified(latest_close=103.0, target_price=100.0)


def test_target_price_rejects_more_than_three_percent_above() -> None:
    assert not is_target_price_qualified(latest_close=103.01, target_price=100.0)


def test_target_price_accepts_unlimited_break_below_target() -> None:
    assert is_target_price_qualified(latest_close=20.0, target_price=100.0)


@pytest.mark.parametrize(
    ("latest_close", "expected"),
    [(90.0, 0.10), (110.0, -0.10)],
)
def test_target_deviation_is_positive_after_break(
    latest_close: float,
    expected: float,
) -> None:
    assert calculate_signed_target_deviation(latest_close, 100.0) == pytest.approx(expected)


def test_post_target_drawdown_uses_lowest_intraday_price() -> None:
    kline_df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04"]),
            "close": [101.0, 99.0, 96.0, 97.0],
            "low": [100.5, 98.0, 91.0, 95.0],
        }
    )

    drawdown = calculate_post_target_drawdown(kline_df, target_price=100.0, start_position=0)

    assert drawdown is not None
    assert drawdown.break_date == datetime(2026, 7, 2)
    assert drawdown.lowest_price == 91.0
    assert drawdown.lowest_date == datetime(2026, 7, 3)
    assert drawdown.decline_rate == pytest.approx(0.09)


def test_post_target_drawdown_is_none_before_target_break() -> None:
    kline_df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-07-01", "2026-07-02"]),
            "close": [103.0, 101.0],
            "low": [100.0, 99.5],
        }
    )

    assert calculate_post_target_drawdown(kline_df, 100.0, 0) is None


def test_key_price_columns_are_adjacent() -> None:
    current_position = OUTPUT_COLUMNS.index("当前价格")

    assert OUTPUT_COLUMNS[current_position : current_position + 3] == [
        "当前价格",
        "1:1等距目标价",
        "1.272扩展目标价",
    ]


def test_three_target_price_columns_are_adjacent() -> None:
    target_position = OUTPUT_COLUMNS.index("1:1等距目标价")

    assert OUTPUT_COLUMNS[target_position : target_position + 3] == [
        "1:1等距目标价",
        "1.272扩展目标价",
        "1.618扩展目标价",
    ]
    assert not any(column.startswith("市值龙头") for column in OUTPUT_COLUMNS)


def test_decline_targets_use_configured_extension_ratios() -> None:
    target_prices = calculate_decline_target_prices(support_level=150.0, peak_price=200.0)

    assert target_prices == pytest.approx((100.0, 86.4, 69.1))
