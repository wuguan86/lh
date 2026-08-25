from __future__ import annotations

import pandas as pd

import board_pattern_screener as screener
from board_screening.enrichment import EnrichmentOutcome
from board_screening.market_data import CachedKlineResult


class FakeEnricher:
    def enrich_record(self, record: dict[str, object]) -> EnrichmentOutcome:
        enriched = dict(record)
        enriched["关联ETF代码"] = "159994"
        enriched["关联ETF名称"] = "5GETF"
        return EnrichmentOutcome(record=enriched, warnings=("股票数据暂缺",))


def test_run_screening_returns_enriched_records_and_latest_trade_date(monkeypatch) -> None:
    board = screener.BoardInfo("概念", "5G")
    kline_df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-07-23", "2026-07-24"]),
            "high": [100.0, 99.0],
            "low": [90.0, 89.0],
            "close": [95.0, 94.0],
        }
    )
    base_record = {
        "板块类型": "概念",
        "板块名称": "5G",
        "最新交易日": "2026-07-24",
        "当前价格": 94.0,
        "1:1等距目标价": 100.0,
        "1.272扩展目标价": 86.4,
        "1.618扩展目标价": 69.1,
        "目标偏离率": "6.00%",
    }
    monkeypatch.setattr(screener, "get_all_boards", lambda *_: [board])
    monkeypatch.setattr(screener, "get_date_range", lambda: ("20260101", "20260724"))
    monkeypatch.setattr(screener, "fetch_board_kline_with_retry", lambda *_: kline_df)
    monkeypatch.setattr(screener, "normalize_kline", lambda frame: frame)
    monkeypatch.setattr(screener, "analyze_board_pattern", lambda *_: base_record)
    monkeypatch.setattr(screener.time, "sleep", lambda *_: None)

    output = screener.run_screening(enricher=FakeEnricher())

    assert output.latest_trade_date == "2026-07-24"
    assert output.records[0]["关联ETF代码"] == "159994"
    assert output.warnings == ("股票数据暂缺",)


def test_historical_run_truncates_cached_kline_at_target_date(monkeypatch) -> None:
    board = screener.BoardInfo("概念", "5G")
    full_frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-07-23", "2026-07-24", "2026-07-27"]),
            "high": [100.0, 99.0, 98.0],
            "low": [90.0, 89.0, 88.0],
            "close": [95.0, 94.0, 93.0],
        }
    )
    analyzed_dates: list[str] = []
    requested_ranges: list[tuple[str, str, str]] = []

    class FakeKlineProvider:
        def load(self, _board, start_date, end_date, required_trade_date):
            requested_ranges.append((start_date, end_date, required_trade_date))
            return CachedKlineResult(full_frame, ())

    def analyze(_board, frame, _min_wave_rise_rate):
        analyzed_dates.extend(frame["date"].dt.strftime("%Y-%m-%d"))
        return None

    monkeypatch.setattr(screener, "get_all_boards", lambda *_: [board])
    monkeypatch.setattr(screener, "analyze_board_pattern", analyze)
    monkeypatch.setattr(screener.time, "sleep", lambda *_: None)

    output = screener.run_screening(
        enricher=FakeEnricher(),
        kline_provider=FakeKlineProvider(),
        required_trade_date="2026-07-24",
    )

    assert requested_ranges[0][1:] == ("20260724", "2026-07-24")
    assert analyzed_dates == ["2026-07-23", "2026-07-24"]
    assert output.latest_trade_date == "2026-07-24"


def test_get_all_boards_reports_single_source_failure(monkeypatch) -> None:
    def fail_industry():
        raise ConnectionError("行业源失败")

    monkeypatch.setattr(screener.ak, "stock_board_industry_name_ths", fail_industry)
    monkeypatch.setattr(
        screener.ak,
        "stock_board_concept_name_ths",
        lambda: pd.DataFrame({"name": ["5G"]}),
    )
    warnings: list[str] = []

    boards = screener.get_all_boards(warnings)

    assert boards == [screener.BoardInfo("概念", "5G")]
    assert len(warnings) == 1
    assert "行业板块列表失败" in warnings[0]


def test_get_all_boards_reports_empty_source_payload(monkeypatch) -> None:
    monkeypatch.setattr(screener.ak, "stock_board_industry_name_ths", lambda: pd.DataFrame())
    monkeypatch.setattr(
        screener.ak,
        "stock_board_concept_name_ths",
        lambda: pd.DataFrame({"name": ["5G"]}),
    )
    warnings: list[str] = []

    boards = screener.get_all_boards(warnings)

    assert boards == [screener.BoardInfo("概念", "5G")]
    assert len(warnings) == 1
    assert "行业板块列表为空" in warnings[0]
