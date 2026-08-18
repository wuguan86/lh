from __future__ import annotations

import pandas as pd

from board_screening.market_data import CachedKlineResult
from board_screening.stock_market_data import StockInfo
import board_screening.stock_screening as stock_screening


class FakeStockProvider:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    def load(self, *_args) -> CachedKlineResult:
        return CachedKlineResult(self.frame, ())


def build_kline_frame() -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=300, freq="B")
    return pd.DataFrame(
        {
            "date": dates,
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "volume": 1000.0,
            "amount": 10000.0,
        }
    )


def test_stock_equal_decline_reuses_rule_and_emits_stock_identity(monkeypatch) -> None:
    stock = StockInfo("600001", "测试股票", 501e8)
    monkeypatch.setattr(stock_screening, "get_date_range", lambda: ("20250101", "20261231"))
    monkeypatch.setattr(stock_screening.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        stock_screening,
        "analyze_board_pattern",
        lambda *_: {
            "股票代码": "600001",
            "股票名称": "测试股票",
            "总市值（亿元）": 501.0,
            "最新交易日": "2026-01-09",
            "当前价格": 10.0,
            "1:1等距目标价": 10.5,
            "目标偏离率": "4.76%",
        },
    )

    output = stock_screening.run_stock_equal_decline_screening(
        FakeStockProvider(build_kline_frame()),
        "2026-01-09",
        stock_provider=lambda _: [stock],
        min_wave_rise_rate=0.1,
    )

    record = output.records[0]
    assert record["股票代码"] == "600001"
    assert record["股票名称"] == "测试股票"
    assert record["总市值（亿元）"] == 501.0
    assert "板块名称" not in record


def test_stock_divergence_emits_all_matched_timeframes(monkeypatch) -> None:
    stock = StockInfo("000001", "测试股票", 600e8)
    frame = build_kline_frame()
    calendar = pd.DataFrame({"trade_date": frame["date"]})
    monkeypatch.setattr(stock_screening.time, "sleep", lambda *_: None)
    monkeypatch.setattr(stock_screening, "get_divergence_date_range", lambda: ("20250101", "20261231"))
    monkeypatch.setattr(
        stock_screening,
        "analyze_macd_divergence",
        lambda _, timeframe, timeframe_frame: {
            "筛选策略": "MACD底背离",
            "股票代码": "000001",
            "股票名称": "测试股票",
            "总市值（亿元）": 600.0,
            "周期": timeframe,
            "背离分类": "单纯底背离",
            "背离次数": 1,
            "最新交易日": timeframe_frame.iloc[-1]["date"].strftime("%Y-%m-%d"),
            "当前价格": 10.0,
        }
        if not timeframe_frame.empty
        else None,
    )

    output = stock_screening.run_stock_divergence_screening(
        FakeStockProvider(frame),
        stock_provider=lambda _: [stock],
        calendar_fetcher=lambda: calendar,
    )

    assert {record["周期"] for record in output.records} == {"日线", "周线", "月线"}
    assert all(record["股票代码"] == "000001" for record in output.records)
