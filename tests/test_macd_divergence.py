from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import board_screening.macd_divergence as divergence
from board_screening.market_data import BoardInfo


def build_calculated_frame(pivot_specs: list[tuple[int, float, float]]) -> pd.DataFrame:
    row_count = 100
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=row_count, freq="D"),
            "open": np.full(row_count, 110.0),
            "high": np.full(row_count, 111.0),
            "low": np.full(row_count, 110.0),
            "close": np.full(row_count, 108.0),
            "volume": np.full(row_count, 1000.0),
            "amount": np.full(row_count, 100000.0),
            "dif": np.zeros(row_count),
            "dea": np.zeros(row_count),
            "macd_histogram": np.full(row_count, 0.2),
        }
    )
    for position, price, dif_value in pivot_specs:
        frame.loc[position, "low"] = price
        frame.loc[position, "dif"] = dif_value
    return frame


def test_macd_uses_standard_double_histogram_formula() -> None:
    frame = pd.DataFrame(
        {
            "close": [10.0, 10.5, 10.2, 11.0, 10.8],
            "date": pd.date_range("2026-01-01", periods=5),
        }
    )

    calculated = divergence.calculate_macd(frame)

    expected_dif = frame["close"].ewm(span=12, adjust=False).mean() - frame["close"].ewm(
        span=26, adjust=False
    ).mean()
    expected_dea = expected_dif.ewm(span=9, adjust=False).mean()
    assert calculated["dif"].to_numpy() == pytest.approx(expected_dif.to_numpy())
    assert calculated["macd_histogram"].to_numpy() == pytest.approx(
        (2 * (expected_dif - expected_dea)).to_numpy()
    )


def test_valid_pair_requires_price_and_dif_rebounds() -> None:
    calculated = build_calculated_frame([(50, 100.0, -2.0), (97, 99.0, -1.0)])
    pivots = divergence.find_divergence_pivots(calculated)

    edges = divergence.build_divergence_edges(calculated, pivots)
    chain = divergence.select_latest_divergence_chain(pivots, edges, len(calculated))

    assert len(edges) == 1
    assert chain is not None
    assert len(chain.edges) == 1

    calculated.loc[51:96, "dif"] = -1.2
    pivots = divergence.find_divergence_pivots(calculated)
    assert divergence.build_divergence_edges(calculated, pivots) == []


def test_pair_rejects_second_low_with_a_lower_interim_low() -> None:
    calculated = build_calculated_frame(
        [(50, 100.0, -3.0), (70, 98.0, -4.0), (97, 99.0, -1.0)]
    )
    pivots = divergence.find_divergence_pivots(calculated)

    # 不能将 50 与 97 配对：70 的价格低点更低，97 不是该波段的新低。
    assert divergence.build_divergence_edges(calculated, pivots) == []


def test_multiple_divergence_builds_two_edge_chain() -> None:
    calculated = build_calculated_frame(
        [(40, 100.0, -3.0), (68, 99.0, -2.0), (97, 98.0, -1.0)]
    )
    pivots = divergence.find_divergence_pivots(calculated)
    edges = divergence.build_divergence_edges(calculated, pivots)

    chain = divergence.select_latest_divergence_chain(pivots, edges, len(calculated))

    assert chain is not None
    assert chain.pivot_indexes == (0, 1, 2)
    assert len(chain.edges) == 2


def test_green_area_must_be_complete_and_shrink_at_least_thirty_percent() -> None:
    histogram = np.array([0.2, -1.0, -2.0, -1.0, 0.1, -1.0, -1.0, 0.2])

    first_area = divergence.calculate_completed_green_area(histogram, 2)
    second_area = divergence.calculate_completed_green_area(histogram, 5)

    assert first_area == (1, 3, 4.0)
    assert second_area == (5, 6, 2.0)
    assert divergence.calculate_completed_green_area(np.array([0.1, -1.0]), 1) is None


def test_analysis_emits_green_red_and_multiple_labels(monkeypatch) -> None:
    calculated = build_calculated_frame(
        [(40, 100.0, -3.0), (68, 99.0, -2.0), (97, 98.0, -1.0)]
    )
    calculated.loc[39:41, "macd_histogram"] = [-1.5, -2.0, -1.5]
    calculated.loc[96:97, "macd_histogram"] = [-1.0, -1.0]
    calculated.loc[98:, "macd_histogram"] = 0.2
    monkeypatch.setattr(divergence, "calculate_macd", lambda _: calculated)

    result = divergence.analyze_macd_divergence(
        BoardInfo("概念", "测试板块"),
        "日线",
        calculated.drop(columns=["dif", "dea", "macd_histogram"]),
    )

    assert result is not None
    assert result["背离次数"] == 2
    assert result["背离分类"] == "线和绿柱双背离、背离+红柱、多次背离"
    assert result["绿柱面积缩小率"] == "60.00%"
    assert result["当前柱状态"] == "红柱"
