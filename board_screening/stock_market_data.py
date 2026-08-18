"""沪深大市值个股列表、前复权行情与增量缓存。"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import akshare as ak
import numpy as np
import pandas as pd

from board_screening.market_data import (
    CACHE_OVERLAP_DAYS,
    KLINE_COLUMNS,
    REQUEST_SLEEP_SECONDS,
    RETRY_TIMES,
    CachedKlineResult,
)


MIN_TOTAL_MARKET_CAP = 30_000_000_000


@dataclass(frozen=True)
class StockInfo:
    code: str
    name: str
    total_market_cap: float


def get_eligible_stocks(
    warnings: list[str] | None = None,
    fetcher: Callable[[], pd.DataFrame] = ak.stock_zh_a_spot_em,
    retry_times: int = RETRY_TIMES,
    retry_delay: float = REQUEST_SLEEP_SECONDS,
) -> list[StockInfo]:
    """获取沪深 A 股，并按总市值严格筛选大于 300 亿元的股票。"""
    warning_messages = warnings if warnings is not None else []
    market_frame: pd.DataFrame | None = None
    last_error: Exception | None = None
    for attempt in range(1, retry_times + 1):
        try:
            market_frame = fetcher()
            break
        except Exception as exc:
            last_error = exc
            logging.warning(
                "获取沪深 A 股市值快照失败，第 %s/%s 次，原因：%s",
                attempt,
                retry_times,
                exc,
            )
            if attempt < retry_times:
                time.sleep(retry_delay * attempt)
    if market_frame is None:
        raise RuntimeError("获取沪深 A 股市值快照失败") from last_error

    required_columns = {"代码", "名称", "总市值"}
    missing_columns = required_columns.difference(market_frame.columns)
    if market_frame.empty or missing_columns:
        raise RuntimeError(f"沪深 A 股市值快照为空或缺少字段：{sorted(missing_columns)}")

    stocks = market_frame[["代码", "名称", "总市值"]].copy()
    stocks["代码"] = stocks["代码"].astype(str).str.extract(r"(\d+)", expand=False).str.zfill(6)
    stocks["名称"] = stocks["名称"].astype(str).str.strip()
    stocks["总市值"] = pd.to_numeric(stocks["总市值"], errors="coerce")
    valid_code = stocks["代码"].str.fullmatch(r"[036]\d{5}", na=False)
    stocks = stocks[
        valid_code
        & (stocks["名称"] != "")
        & (stocks["总市值"] > MIN_TOTAL_MARKET_CAP)
    ].copy()
    stocks.drop_duplicates(subset=["代码"], keep="first", inplace=True)
    stocks.sort_values("代码", inplace=True)
    logging.info("沪深 A 股总市值大于 300 亿元的股票共 %s 只。", len(stocks))
    if stocks.empty:
        warning_messages.append("沪深 A 股市值快照中没有总市值大于 300 亿元的股票")
    return [
        StockInfo(str(row.代码), str(row.名称), float(row.总市值))
        for row in stocks.itertuples(index=False)
    ]


def fetch_stock_kline(stock: StockInfo, start_date: str, end_date: str) -> pd.DataFrame:
    """获取单只股票的前复权日线行情。"""
    return ak.stock_zh_a_hist(
        symbol=stock.code,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust="qfq",
    )


def fetch_stock_kline_with_retry(
    stock: StockInfo,
    start_date: str,
    end_date: str,
    fetcher: Callable[[StockInfo, str, str], pd.DataFrame] = fetch_stock_kline,
) -> pd.DataFrame | None:
    """重试股票行情请求，连续失败后交由上层跳过该股票。"""
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            return fetcher(stock, start_date, end_date)
        except Exception as exc:
            logging.warning(
                "获取【%s %s】前复权日 K 失败，第 %s/%s 次，原因：%s",
                stock.code,
                stock.name,
                attempt,
                RETRY_TIMES,
                exc,
            )
            if attempt < RETRY_TIMES:
                time.sleep(REQUEST_SLEEP_SECONDS * attempt)
    logging.error("【%s %s】前复权日 K 连续获取失败。", stock.code, stock.name)
    return None


def normalize_stock_kline(raw_frame: pd.DataFrame) -> pd.DataFrame:
    """将个股行情字段转换为筛选算法使用的统一格式。"""
    column_map = {
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
    }
    missing_columns = [column for column in column_map if column not in raw_frame.columns]
    if missing_columns:
        raise ValueError(f"个股日 K 数据缺少必要字段：{missing_columns}")
    frame = raw_frame[list(column_map)].rename(columns=column_map).copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in KLINE_COLUMNS[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame.dropna(subset=["date", "high", "low", "close"], inplace=True)
    frame.sort_values("date", inplace=True)
    frame.drop_duplicates(subset=["date"], keep="last", inplace=True)
    frame.reset_index(drop=True, inplace=True)
    return frame


class StockKlineCache:
    """按股票代码持久化前复权日线。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS stock_klines (
                    stock_code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open REAL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL,
                    amount REAL,
                    PRIMARY KEY (stock_code, trade_date)
                );
                CREATE INDEX IF NOT EXISTS idx_stock_klines_date
                    ON stock_klines(trade_date);
                """
            )

    def get_bounds(self, stock_code: str) -> tuple[str | None, str | None]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
                FROM stock_klines WHERE stock_code = ?
                """,
                (stock_code,),
            ).fetchone()
        return row["first_date"], row["last_date"]

    def load(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        normalized_start = pd.Timestamp(start_date).strftime("%Y-%m-%d")
        normalized_end = pd.Timestamp(end_date).strftime("%Y-%m-%d")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date AS date, open, high, low, close, volume, amount
                FROM stock_klines
                WHERE stock_code = ? AND trade_date BETWEEN ? AND ?
                ORDER BY trade_date
                """,
                (stock_code, normalized_start, normalized_end),
            ).fetchall()
        frame = pd.DataFrame([dict(row) for row in rows], columns=KLINE_COLUMNS)
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"])
        return frame

    def upsert(self, stock_code: str, frame: pd.DataFrame) -> None:
        if frame.empty:
            return
        rows = [
            (
                stock_code,
                pd.Timestamp(row.date).strftime("%Y-%m-%d"),
                row.open,
                row.high,
                row.low,
                row.close,
                row.volume,
                row.amount,
            )
            for row in frame.itertuples(index=False)
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO stock_klines (
                    stock_code, trade_date, open, high, low, close, volume, amount
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                    open = excluded.open, high = excluded.high, low = excluded.low,
                    close = excluded.close, volume = excluded.volume, amount = excluded.amount
                """,
                rows,
            )

    def replace(self, stock_code: str, frame: pd.DataFrame) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM stock_klines WHERE stock_code = ?", (stock_code,))
        self.upsert(stock_code, frame)


class CachedStockKlineProvider:
    """增量补齐个股行情，并在复权变化时重建单股缓存。"""

    def __init__(
        self,
        cache: StockKlineCache,
        fetcher: Callable[[StockInfo, str, str], pd.DataFrame | None] = fetch_stock_kline_with_retry,
    ) -> None:
        self.cache = cache
        self.fetcher = fetcher

    def load(
        self,
        stock: StockInfo,
        start_date: str,
        end_date: str,
        required_trade_date: str,
    ) -> CachedKlineResult:
        first_date, last_date = self.cache.get_bounds(stock.code)
        intervals = self._missing_intervals(start_date, end_date, first_date, last_date)
        warnings: list[str] = []
        for interval_start, interval_end in intervals:
            existing_overlap = self.cache.load(stock.code, interval_start, interval_end)
            raw_frame = self.fetcher(stock, interval_start, interval_end)
            if raw_frame is None:
                warnings.append(
                    f"【{stock.code} {stock.name}】行情区间 {interval_start}-{interval_end} 获取失败"
                )
                continue
            try:
                fetched_frame = normalize_stock_kline(raw_frame)
                if self._has_adjustment_change(existing_overlap, fetched_frame):
                    full_raw_frame = self.fetcher(stock, start_date, end_date)
                    if full_raw_frame is None:
                        warnings.append(f"【{stock.code} {stock.name}】复权变化后完整行情回填失败")
                        return CachedKlineResult(None, tuple(warnings))
                    self.cache.replace(stock.code, normalize_stock_kline(full_raw_frame))
                    logging.info("【%s %s】前复权因子变化，已重建行情缓存。", stock.code, stock.name)
                    break
                self.cache.upsert(stock.code, fetched_frame)
            except Exception as exc:
                logging.exception("缓存【%s %s】行情失败，原因：%s", stock.code, stock.name, exc)
                warnings.append(f"【{stock.code} {stock.name}】行情缓存失败：{exc}")

        cached_frame = self.cache.load(stock.code, start_date, end_date)
        if cached_frame.empty:
            return CachedKlineResult(None, tuple(warnings))
        latest_date = cached_frame.iloc[-1]["date"].strftime("%Y-%m-%d")
        if latest_date < required_trade_date:
            warnings.append(
                f"【{stock.code} {stock.name}】行情仅更新至 {latest_date}，"
                f"未覆盖目标交易日 {required_trade_date}"
            )
            return CachedKlineResult(None, tuple(warnings))
        return CachedKlineResult(cached_frame, tuple(warnings))

    @staticmethod
    def _has_adjustment_change(existing: pd.DataFrame, fetched: pd.DataFrame) -> bool:
        if existing.empty or fetched.empty:
            return False
        comparison = existing[["date", "close"]].merge(
            fetched[["date", "close"]],
            on="date",
            suffixes=("_old", "_new"),
        )
        if comparison.empty:
            return False
        return not bool(
            np.allclose(
                comparison["close_old"].to_numpy(dtype=float),
                comparison["close_new"].to_numpy(dtype=float),
                rtol=0,
                atol=0.0001,
            )
        )

    @staticmethod
    def _missing_intervals(
        start_date: str,
        end_date: str,
        first_date: str | None,
        last_date: str | None,
    ) -> list[tuple[str, str]]:
        requested_start = pd.Timestamp(start_date)
        requested_end = pd.Timestamp(end_date)
        if first_date is None or last_date is None:
            return [(requested_start.strftime("%Y%m%d"), requested_end.strftime("%Y%m%d"))]

        intervals: list[tuple[str, str]] = []
        cached_start = pd.Timestamp(first_date)
        cached_end = pd.Timestamp(last_date)
        if cached_start > requested_start:
            leading_end = cached_start - pd.Timedelta(days=1)
            intervals.append((requested_start.strftime("%Y%m%d"), leading_end.strftime("%Y%m%d")))
        trailing_start = max(requested_start, cached_end - pd.Timedelta(days=CACHE_OVERLAP_DAYS))
        if trailing_start <= requested_end:
            intervals.append((trailing_start.strftime("%Y%m%d"), requested_end.strftime("%Y%m%d")))
        return intervals
