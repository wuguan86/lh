from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import board_pattern_screener as screener


def build_local_low_frame() -> pd.DataFrame:
    low_values = np.full(40, 100.0)
    low_values[12] = 80.0
    low_values[26] = 95.0
    return pd.DataFrame({"low": low_values})


def test_small_wave_falls_back_to_earlier_qualified_local_low() -> None:
    lookback_df = build_local_low_frame()

    support_position = screener.find_nearest_left_local_low(
        lookback_df,
        peak_position=39,
        peak_price=100.0,
        min_wave_rise_rate=0.10,
    )

    assert support_position == 12


def test_board_is_filtered_when_no_local_low_reaches_minimum_rise() -> None:
    lookback_df = build_local_low_frame()

    support_position = screener.find_nearest_left_local_low(
        lookback_df,
        peak_position=39,
        peak_price=100.0,
        min_wave_rise_rate=0.30,
    )

    assert support_position is None


def test_min_wave_rise_percent_can_be_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIN_WAVE_RISE_PERCENT", "12.5")

    assert screener.get_min_wave_rise_rate() == pytest.approx(0.125)


def test_analyze_accepts_deep_target_break_and_reports_drawdown(monkeypatch: pytest.MonkeyPatch) -> None:
    dates = pd.date_range("2026-01-01", periods=90, freq="D")
    close_values = np.linspace(180.0, 50.0, 90)
    kline_df = pd.DataFrame(
        {
            "date": dates,
            "open": close_values + 1,
            "high": close_values + 2,
            "low": close_values - 2,
            "close": close_values,
            "volume": np.full(90, 1000.0),
            "amount": np.full(90, 100000.0),
        }
    )
    kline_df.loc[20, "high"] = 200.0
    kline_df.loc[10, "low"] = 150.0
    kline_df.loc[21:69, "close"] = 120.0
    kline_df.loc[21:69, "low"] = 118.0
    kline_df.loc[70, "close"] = 99.0
    kline_df.loc[75, "low"] = 40.0

    monkeypatch.setattr(screener, "find_nearest_left_local_low", lambda *_: 10)
    monkeypatch.setattr(screener, "find_first_break_support_position", lambda *_: 60)
    monkeypatch.setattr(screener, "calculate_break_period_metrics", lambda *_: (30, 25, 0.8, 0))
    monkeypatch.setattr(screener, "calculate_latest_bias", lambda *_: 0.2)

    result = screener.analyze_board_pattern(screener.BoardInfo("概念", "测试板块"), kline_df)

    assert result is not None
    assert result["目标位价格"] == 100.0
    assert result["目标偏离率"] == "50.00%"
    assert result["上涨幅度"] == "33.33%"
    assert result["首次跌破目标日期"] == dates[70].strftime("%Y-%m-%d")
    assert result["跌破目标后最低价"] == 40.0
    assert result["最低价日期"] == dates[75].strftime("%Y-%m-%d")
    assert result["最大跌幅"] == "60.00%"
