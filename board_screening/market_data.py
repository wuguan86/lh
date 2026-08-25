"""板块行情获取、缓存与多周期聚合。"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import akshare as ak
import pandas as pd


BOARD_TYPE_INDUSTRY = "行业"
BOARD_TYPE_CONCEPT = "概念"
RETRY_TIMES = 3
REQUEST_SLEEP_SECONDS = 0.8
CACHE_OVERLAP_DAYS = 7
KLINE_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]


@dataclass(frozen=True)
class BoardInfo:
    board_type: str
    board_name: str


@dataclass(frozen=True)
class CachedKlineResult:
    frame: pd.DataFrame | None
    warnings: tuple[str, ...]


def get_all_boards(warnings: list[str] | None = None) -> list[BoardInfo]:
    """获取同花顺行业和概念板块，单一来源失败时继续处理另一来源。"""
    warning_messages = warnings if warnings is not None else []
    board_frames: list[pd.DataFrame] = []
    sources: list[tuple[str, Callable[[], pd.DataFrame]]] = [
        (BOARD_TYPE_INDUSTRY, ak.stock_board_industry_name_ths),
        (BOARD_TYPE_CONCEPT, ak.stock_board_concept_name_ths),
    ]
    for board_type, fetcher in sources:
        try:
            source_frame = fetcher()
            if source_frame.empty or "name" not in source_frame.columns:
                message = f"获取{board_type}板块列表为空或缺少 name 字段，已跳过。"
                logging.warning(message)
                warning_messages.append(message)
                continue
            board_frame = source_frame[["name"]].copy()
            board_frame["board_type"] = board_type
            board_frame.rename(columns={"name": "board_name"}, inplace=True)
            board_frames.append(board_frame)
            logging.info("已获取%s板块列表，共 %s 个。", board_type, len(board_frame))
        except Exception as exc:
            logging.exception("获取%s板块列表失败，原因：%s", board_type, exc)
            warning_messages.append(f"获取{board_type}板块列表失败：{exc}")

    if not board_frames:
        return []
    boards_frame = pd.concat(board_frames, ignore_index=True)
    boards_frame["board_name"] = boards_frame["board_name"].astype(str).str.strip()
    boards_frame = boards_frame[boards_frame["board_name"] != ""]
    boards_frame.drop_duplicates(subset=["board_type", "board_name"], inplace=True)
    return [
        BoardInfo(row.board_type, row.board_name)
        for row in boards_frame.itertuples(index=False)
    ]


def get_date_range(calendar_days: int = 220, end_date: date | None = None) -> tuple[str, str]:
    """按自然日生成行情请求区间。"""
    end_day = end_date or date.today()
    start_day = end_day - timedelta(days=calendar_days)
    return start_day.strftime("%Y%m%d"), end_day.strftime("%Y%m%d")


def fetch_board_kline(board: BoardInfo, start_date: str, end_date: str) -> pd.DataFrame:
    """按板块类型调用对应 AKShare 接口。"""
    if board.board_type == BOARD_TYPE_INDUSTRY:
        return ak.stock_board_industry_index_ths(
            symbol=board.board_name,
            start_date=start_date,
            end_date=end_date,
        )
    if board.board_type == BOARD_TYPE_CONCEPT:
        return ak.stock_board_concept_index_ths(
            symbol=board.board_name,
            start_date=start_date,
            end_date=end_date,
        )
    raise ValueError(f"未知板块类型：{board.board_type}")


def fetch_board_kline_with_retry(
    board: BoardInfo,
    start_date: str,
    end_date: str,
    fetcher: Callable[[BoardInfo, str, str], pd.DataFrame] = fetch_board_kline,
) -> pd.DataFrame | None:
    """重试单个板块行情，连续失败后返回空结果供上层决定是否回退缓存。"""
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            return fetcher(board, start_date, end_date)
        except Exception as exc:
            logging.warning(
                "获取【%s-%s】日 K 失败，第 %s/%s 次尝试，原因：%s",
                board.board_type,
                board.board_name,
                attempt,
                RETRY_TIMES,
                exc,
            )
            if attempt < RETRY_TIMES:
                time.sleep(REQUEST_SLEEP_SECONDS * attempt)
    logging.error("【%s-%s】连续获取失败。", board.board_type, board.board_name)
    return None


def normalize_kline(raw_frame: pd.DataFrame) -> pd.DataFrame:
    """统一行情字段并清理无法参与计算的记录。"""
    column_map = {
        "日期": "date",
        "开盘价": "open",
        "最高价": "high",
        "最低价": "low",
        "收盘价": "close",
        "成交量": "volume",
        "成交额": "amount",
    }
    missing_columns = [column for column in column_map if column not in raw_frame.columns]
    if missing_columns:
        raise ValueError(f"日 K 数据缺少必要字段：{missing_columns}")
    kline_frame = raw_frame[list(column_map)].rename(columns=column_map).copy()
    kline_frame["date"] = pd.to_datetime(kline_frame["date"], errors="coerce")
    for column in KLINE_COLUMNS[1:]:
        kline_frame[column] = pd.to_numeric(kline_frame[column], errors="coerce")
    kline_frame.dropna(subset=["date", "high", "low", "close"], inplace=True)
    kline_frame.sort_values("date", inplace=True)
    kline_frame.drop_duplicates(subset=["date"], keep="last", inplace=True)
    kline_frame.reset_index(drop=True, inplace=True)
    return kline_frame


class KlineCache:
    """在应用数据库中持久化板块日线，避免每天重复下载多年历史。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS board_klines (
                    board_type TEXT NOT NULL,
                    board_name TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open REAL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL,
                    amount REAL,
                    PRIMARY KEY (board_type, board_name, trade_date)
                );
                CREATE INDEX IF NOT EXISTS idx_board_klines_date
                    ON board_klines(trade_date);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def get_bounds(self, board: BoardInfo) -> tuple[str | None, str | None]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
                FROM board_klines WHERE board_type = ? AND board_name = ?
                """,
                (board.board_type, board.board_name),
            ).fetchone()
        return row["first_date"], row["last_date"]

    def upsert(self, board: BoardInfo, frame: pd.DataFrame) -> None:
        if frame.empty:
            return
        rows = [
            (
                board.board_type,
                board.board_name,
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
                INSERT INTO board_klines (
                    board_type, board_name, trade_date, open, high, low, close, volume, amount
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(board_type, board_name, trade_date) DO UPDATE SET
                    open = excluded.open, high = excluded.high, low = excluded.low,
                    close = excluded.close, volume = excluded.volume, amount = excluded.amount
                """,
                rows,
            )

    def load(self, board: BoardInfo, start_date: str, end_date: str) -> pd.DataFrame:
        normalized_start = pd.Timestamp(start_date).strftime("%Y-%m-%d")
        normalized_end = pd.Timestamp(end_date).strftime("%Y-%m-%d")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date AS date, open, high, low, close, volume, amount
                FROM board_klines
                WHERE board_type = ? AND board_name = ? AND trade_date BETWEEN ? AND ?
                ORDER BY trade_date
                """,
                (board.board_type, board.board_name, normalized_start, normalized_end),
            ).fetchall()
        frame = pd.DataFrame([dict(row) for row in rows], columns=KLINE_COLUMNS)
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"])
        return frame


class CachedKlineProvider:
    """按目标区间补齐缓存，并确保返回数据覆盖本次目标交易日。"""

    def __init__(
        self,
        cache: KlineCache,
        fetcher: Callable[[BoardInfo, str, str], pd.DataFrame | None] = fetch_board_kline_with_retry,
    ) -> None:
        self.cache = cache
        self.fetcher = fetcher

    def load(
        self,
        board: BoardInfo,
        start_date: str,
        end_date: str,
        required_trade_date: str,
    ) -> CachedKlineResult:
        first_date, last_date = self.cache.get_bounds(board)
        intervals = self._missing_intervals(start_date, end_date, first_date, last_date)
        warning_messages: list[str] = []
        for interval_start, interval_end in intervals:
            raw_frame = self.fetcher(board, interval_start, interval_end)
            if raw_frame is None:
                warning_messages.append(
                    f"【{board.board_type}-{board.board_name}】行情区间"
                    f" {interval_start}-{interval_end} 获取失败"
                )
                continue
            try:
                self.cache.upsert(board, normalize_kline(raw_frame))
            except Exception as exc:
                logging.exception("缓存【%s-%s】行情失败，原因：%s", board.board_type, board.board_name, exc)
                warning_messages.append(f"【{board.board_type}-{board.board_name}】行情缓存失败：{exc}")

        cached_frame = self.cache.load(board, start_date, end_date)
        if cached_frame.empty:
            return CachedKlineResult(None, tuple(warning_messages))
        latest_date = cached_frame.iloc[-1]["date"].strftime("%Y-%m-%d")
        if latest_date < required_trade_date:
            warning_messages.append(
                f"【{board.board_type}-{board.board_name}】缓存仅更新至 {latest_date}，"
                f"未覆盖目标交易日 {required_trade_date}"
            )
            return CachedKlineResult(None, tuple(warning_messages))
        return CachedKlineResult(cached_frame, tuple(warning_messages))

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


def aggregate_completed_timeframes(
    daily_frame: pd.DataFrame,
    trade_calendar: pd.DataFrame,
    latest_trade_date: str,
) -> dict[str, pd.DataFrame]:
    """生成日、周、月 K 线，仅保留交易日历确认已经结束的周期。"""
    if trade_calendar.empty or "trade_date" not in trade_calendar.columns:
        raise ValueError("交易日历为空或缺少 trade_date 字段")
    latest_date = pd.Timestamp(latest_trade_date)
    daily = daily_frame[daily_frame["date"] <= latest_date].copy().reset_index(drop=True)
    if daily.empty:
        return {"日线": daily, "周线": daily.copy(), "月线": daily.copy()}

    calendar_dates = pd.to_datetime(trade_calendar["trade_date"], errors="coerce").dropna()
    completed_week_dates = calendar_dates.groupby(calendar_dates.dt.to_period("W-FRI")).max()
    completed_month_dates = calendar_dates.groupby(calendar_dates.dt.to_period("M")).max()
    weekly = _aggregate_period(daily, "W-FRI", completed_week_dates, latest_date)
    monthly = _aggregate_period(daily, "M", completed_month_dates, latest_date)
    return {"日线": daily, "周线": weekly, "月线": monthly}


def _aggregate_period(
    daily_frame: pd.DataFrame,
    frequency: str,
    period_last_trade_dates: pd.Series,
    latest_date: pd.Timestamp,
) -> pd.DataFrame:
    working_frame = daily_frame.copy()
    working_frame["period"] = working_frame["date"].dt.to_period(frequency)
    aggregated = working_frame.groupby("period", sort=True).agg(
        date=("date", "max"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        amount=("amount", "sum"),
    )
    expected_dates = aggregated.index.map(period_last_trade_dates.to_dict())
    complete_mask = (aggregated["date"].to_numpy() == expected_dates) & (expected_dates <= latest_date)
    return aggregated.loc[complete_mask, KLINE_COLUMNS].reset_index(drop=True)
