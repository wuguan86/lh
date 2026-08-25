"""MACD 多周期底背离全市场筛选流程。"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Callable

import akshare as ak
import pandas as pd

from board_screening.enrichment import DataEnricher
from board_screening.macd_divergence import analyze_macd_divergence
from board_screening.market_data import (
    REQUEST_SLEEP_SECONDS,
    BoardInfo,
    CachedKlineProvider,
    aggregate_completed_timeframes,
    get_all_boards,
)
from board_screening.models import ScreeningOutput
from board_screening.scheduler import resolve_screening_trade_date


HISTORY_YEARS = 12


def get_divergence_date_range(today: date | None = None) -> tuple[str, str]:
    """回填十二年日线，为月线 MACD 留出足够预热区间。"""
    end_day = pd.Timestamp(today or date.today())
    start_day = end_day - pd.DateOffset(years=HISTORY_YEARS)
    return start_day.strftime("%Y%m%d"), end_day.strftime("%Y%m%d")


def run_divergence_screening(
    kline_provider: CachedKlineProvider,
    enricher: DataEnricher | None = None,
    board_provider: Callable[[list[str] | None], list[BoardInfo]] = get_all_boards,
    calendar_fetcher: Callable[[], pd.DataFrame] = ak.tool_trade_date_hist_sina,
    target_trade_date: str | None = None,
) -> ScreeningOutput:
    """遍历行业和概念板块并输出日、周、月三个周期的底背离结果。"""
    warning_messages: list[str] = []
    boards = board_provider(warning_messages)
    if not boards:
        raise RuntimeError("未获取到任何同花顺板块")
    trade_calendar = calendar_fetcher()
    latest_trade_date = resolve_screening_trade_date(trade_calendar, target_trade_date)
    start_date, end_date = get_divergence_date_range(
        datetime.strptime(latest_trade_date, "%Y-%m-%d").date()
    )
    logging.info(
        "开始执行 MACD 底背离筛选，共 %s 个板块，请求区间 %s 至 %s。",
        len(boards),
        start_date,
        end_date,
    )

    matched_records: list[dict[str, object]] = []
    processed_board_count = 0
    for index, board in enumerate(boards, start=1):
        logging.info(
            "[%s/%s] 正在筛选【%s】%s 的 MACD 底背离。",
            index,
            len(boards),
            board.board_type,
            board.board_name,
        )
        cached_result = kline_provider.load(
            board,
            start_date,
            end_date,
            latest_trade_date,
        )
        warning_messages.extend(cached_result.warnings)
        if cached_result.frame is None:
            time.sleep(REQUEST_SLEEP_SECONDS)
            continue
        processed_board_count += 1
        try:
            timeframe_frames = aggregate_completed_timeframes(
                cached_result.frame,
                trade_calendar,
                latest_trade_date,
            )
            for timeframe, timeframe_frame in timeframe_frames.items():
                matched_record = analyze_macd_divergence(board, timeframe, timeframe_frame)
                if matched_record is not None:
                    matched_records.append(matched_record)
                    logging.info(
                        "命中【%s-%s】%s：%s，背离 %s 次。",
                        board.board_type,
                        board.board_name,
                        timeframe,
                        matched_record["背离分类"],
                        matched_record["背离次数"],
                    )
        except Exception as exc:
            logging.exception("分析【%s-%s】底背离失败，原因：%s", board.board_type, board.board_name, exc)
            warning_messages.append(f"【{board.board_type}-{board.board_name}】底背离分析失败：{exc}")
        time.sleep(REQUEST_SLEEP_SECONDS)

    if processed_board_count == 0:
        raise RuntimeError("所有板块行情均获取失败")

    data_enricher = enricher or DataEnricher()
    enriched_records: list[dict[str, object]] = []
    for record in matched_records:
        outcome = data_enricher.enrich_record(record)
        enriched_records.append(outcome.record)
        warning_messages.extend(outcome.warnings)
    return ScreeningOutput(
        records=tuple(enriched_records),
        warnings=tuple(warning_messages),
        latest_trade_date=latest_trade_date,
    )
