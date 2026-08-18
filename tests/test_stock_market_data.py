from __future__ import annotations

import pandas as pd

from board_screening.stock_market_data import (
    CachedStockKlineProvider,
    StockInfo,
    StockKlineCache,
    fetch_stock_kline,
    get_eligible_stocks,
    normalize_stock_kline,
)


def build_raw_stock_frame(close: float = 10.0) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", "2026-01-09", freq="B")
    return pd.DataFrame(
        {
            "日期": dates,
            "开盘": close,
            "收盘": close,
            "最高": close + 1,
            "最低": close - 1,
            "成交量": 1000.0,
            "成交额": 10000.0,
        }
    )


def test_eligible_stocks_filter_market_cap_exchange_boundary_and_duplicates() -> None:
    market_frame = pd.DataFrame(
        {
            "代码": ["000001", "300001", "600001", "600001", "830001", "200001"],
            "名称": ["深市", "临界", "沪市", "重复", "北交所", "深市B股"],
            "总市值": [301e8, 300e8, 500e8, 600e8, 800e8, 900e8],
        }
    )

    stocks = get_eligible_stocks(fetcher=lambda: market_frame, retry_delay=0)

    assert [(stock.code, stock.name) for stock in stocks] == [
        ("000001", "深市"),
        ("600001", "沪市"),
    ]


def test_eligible_stocks_reject_missing_required_columns() -> None:
    market_frame = pd.DataFrame({"代码": ["600001"], "名称": ["沪市"]})

    try:
        get_eligible_stocks(fetcher=lambda: market_frame, retry_delay=0)
    except RuntimeError as exc:
        assert "缺少字段" in str(exc)
    else:
        raise AssertionError("缺少总市值字段时应拒绝数据")


def test_stock_history_uses_front_adjustment(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_history(**kwargs):
        calls.append(kwargs)
        return build_raw_stock_frame()

    monkeypatch.setattr("board_screening.stock_market_data.ak.stock_zh_a_hist", fake_history)
    stock = StockInfo("600001", "沪市", 500e8)

    fetch_stock_kline(stock, "20260101", "20260109")

    assert calls[0]["adjust"] == "qfq"
    assert calls[0]["symbol"] == "600001"


def test_normalize_stock_history_to_common_columns() -> None:
    normalized = normalize_stock_kline(build_raw_stock_frame())

    assert normalized.columns.tolist() == [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]
    assert normalized.iloc[-1]["close"] == 10.0


def test_stock_cache_rebuilds_when_adjustment_changes(tmp_path) -> None:
    cache = StockKlineCache(tmp_path / "screening.db")
    cache.initialize()
    stock = StockInfo("600001", "沪市", 500e8)
    cache.upsert(stock.code, normalize_stock_kline(build_raw_stock_frame(10.0)))
    fetch_calls: list[tuple[str, str]] = []

    def fetcher(_: StockInfo, start_date: str, end_date: str) -> pd.DataFrame:
        fetch_calls.append((start_date, end_date))
        return build_raw_stock_frame(11.0)

    result = CachedStockKlineProvider(cache, fetcher).load(
        stock,
        "20260101",
        "20260109",
        "2026-01-09",
    )

    assert result.frame is not None
    assert len(fetch_calls) == 2
    assert set(result.frame["close"]) == {11.0}


def test_stock_cache_skips_stale_suspended_stock(tmp_path) -> None:
    cache = StockKlineCache(tmp_path / "screening.db")
    cache.initialize()
    stock = StockInfo("600001", "沪市", 500e8)
    provider = CachedStockKlineProvider(cache, lambda *_: build_raw_stock_frame())

    result = provider.load(stock, "20260101", "20260110", "2026-01-10")

    assert result.frame is None
    assert "未覆盖目标交易日" in result.warnings[-1]
