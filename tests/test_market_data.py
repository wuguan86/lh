from __future__ import annotations

import pandas as pd

from board_screening.market_data import (
    BoardInfo,
    CachedKlineProvider,
    KlineCache,
    aggregate_completed_timeframes,
)


def build_raw_frame() -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", "2026-01-09", freq="B")
    return pd.DataFrame(
        {
            "日期": dates,
            "开盘价": 10.0,
            "最高价": 11.0,
            "最低价": 9.0,
            "收盘价": 10.5,
            "成交量": 1000.0,
            "成交额": 10000.0,
        }
    )


def test_cache_upserts_overlap_and_falls_back_when_latest_is_covered(tmp_path) -> None:
    cache = KlineCache(tmp_path / "screening.db")
    cache.initialize()
    board = BoardInfo("概念", "测试板块")
    fetch_calls: list[tuple[str, str]] = []

    def fetcher(_: BoardInfo, start_date: str, end_date: str) -> pd.DataFrame:
        fetch_calls.append((start_date, end_date))
        return build_raw_frame()

    provider = CachedKlineProvider(cache, fetcher)
    first_result = provider.load(board, "20260101", "20260110", "2026-01-09")
    second_result = provider.load(board, "20260101", "20260110", "2026-01-09")

    assert first_result.frame is not None
    assert second_result.frame is not None
    assert len(second_result.frame) == 7
    assert len(fetch_calls) == 2

    fallback_provider = CachedKlineProvider(cache, lambda *_: None)
    fallback_result = fallback_provider.load(board, "20260101", "20260110", "2026-01-09")
    assert fallback_result.frame is not None
    assert fallback_result.warnings


def test_period_aggregation_excludes_unfinished_week_and_month() -> None:
    dates = pd.date_range("2026-05-01", "2026-07-29", freq="B")
    daily_frame = pd.DataFrame(
        {
            "date": dates,
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 1000.0,
            "amount": 10000.0,
        }
    )
    trade_calendar = pd.DataFrame(
        {"trade_date": pd.date_range("2026-01-01", "2026-12-31", freq="B")}
    )

    timeframes = aggregate_completed_timeframes(
        daily_frame,
        trade_calendar,
        "2026-07-29",
    )

    assert timeframes["日线"].iloc[-1]["date"] == pd.Timestamp("2026-07-29")
    assert timeframes["周线"].iloc[-1]["date"] == pd.Timestamp("2026-07-24")
    assert timeframes["月线"].iloc[-1]["date"] == pd.Timestamp("2026-06-30")
