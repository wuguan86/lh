from __future__ import annotations

import pandas as pd

import board_screening.divergence_screening as screening
from board_screening.market_data import BoardInfo, CachedKlineResult


def test_historical_divergence_uses_target_date_for_range_and_aggregation(monkeypatch) -> None:
    board = BoardInfo("概念", "5G")
    daily_frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-07-23", "2026-07-24", "2026-07-27"]),
            "open": [95.0, 94.0, 93.0],
            "high": [100.0, 99.0, 98.0],
            "low": [90.0, 89.0, 88.0],
            "close": [95.0, 94.0, 93.0],
            "volume": [1000.0, 1000.0, 1000.0],
            "amount": [100000.0, 100000.0, 100000.0],
        }
    )
    calendar = pd.DataFrame(
        {"trade_date": pd.to_datetime(["2026-07-23", "2026-07-24", "2026-07-27"])}
    )
    requested_ranges: list[tuple[str, str, str]] = []
    aggregation_dates: list[str] = []

    class FakeKlineProvider:
        def load(self, _board, start_date, end_date, required_trade_date):
            requested_ranges.append((start_date, end_date, required_trade_date))
            return CachedKlineResult(daily_frame, ())

    def aggregate(frame, _calendar, latest_trade_date):
        aggregation_dates.append(latest_trade_date)
        truncated = frame[frame["date"] <= pd.Timestamp(latest_trade_date)]
        return {"日线": truncated, "周线": truncated, "月线": truncated}

    monkeypatch.setattr(screening, "aggregate_completed_timeframes", aggregate)
    monkeypatch.setattr(screening, "analyze_macd_divergence", lambda *_: None)
    monkeypatch.setattr(screening.time, "sleep", lambda *_: None)

    output = screening.run_divergence_screening(
        FakeKlineProvider(),
        board_provider=lambda _warnings: [board],
        calendar_fetcher=lambda: calendar,
        target_trade_date="2026-07-24",
    )

    assert requested_ranges[0][1:] == ("20260724", "2026-07-24")
    assert aggregation_dates == ["2026-07-24"]
    assert output.latest_trade_date == "2026-07-24"
