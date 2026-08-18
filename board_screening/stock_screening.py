"""沪深大市值个股的等距下跌与 MACD 底背离筛选流程。"""

from __future__ import annotations

import logging
import time
from typing import Callable

import akshare as ak
import pandas as pd

from board_pattern_screener import analyze_board_pattern, get_date_range, get_min_wave_rise_rate
from board_screening.divergence_screening import get_divergence_date_range
from board_screening.macd_divergence import analyze_macd_divergence
from board_screening.market_data import REQUEST_SLEEP_SECONDS, aggregate_completed_timeframes
from board_screening.models import ScreeningOutput
from board_screening.scheduler import fetch_latest_trade_date
from board_screening.stock_market_data import (
    CachedStockKlineProvider,
    StockInfo,
    get_eligible_stocks,
)


StockProvider = Callable[[list[str] | None], list[StockInfo]]


def run_stock_equal_decline_screening(
    kline_provider: CachedStockKlineProvider,
    required_trade_date: str,
    stock_provider: StockProvider = get_eligible_stocks,
    min_wave_rise_rate: float | None = None,
) -> ScreeningOutput:
    """遍历沪深大市值个股并执行现有等距下跌规则。"""
    warning_messages: list[str] = []
    stocks = stock_provider(warning_messages)
    if not stocks:
        raise RuntimeError("未获取到总市值大于 300 亿元的沪深 A 股")
    start_date, end_date = get_date_range()
    configured_min_wave_rise_rate = (
        get_min_wave_rise_rate() if min_wave_rise_rate is None else min_wave_rise_rate
    )
    logging.info(
        "开始执行个股等距下跌筛选，共 %s 只股票，请求区间 %s 至 %s。",
        len(stocks),
        start_date,
        end_date,
    )

    matched_records: list[dict[str, object]] = []
    processed_stock_count = 0
    for index, stock in enumerate(stocks, start=1):
        logging.info("[%s/%s] 正在筛选【%s %s】等距下跌。", index, len(stocks), stock.code, stock.name)
        cached_result = kline_provider.load(stock, start_date, end_date, required_trade_date)
        warning_messages.extend(cached_result.warnings)
        if cached_result.frame is None:
            time.sleep(REQUEST_SLEEP_SECONDS)
            continue
        processed_stock_count += 1
        try:
            matched_record = analyze_board_pattern(
                stock,
                cached_result.frame,
                configured_min_wave_rise_rate,
            )
            if matched_record is not None:
                matched_records.append(matched_record)
                logging.info(
                    "命中【%s %s】个股等距下跌，当前价 %s，目标偏离率 %s。",
                    stock.code,
                    stock.name,
                    matched_record["当前价格"],
                    matched_record["目标偏离率"],
                )
        except Exception as exc:
            logging.exception("分析【%s %s】等距下跌失败，原因：%s", stock.code, stock.name, exc)
            warning_messages.append(f"【{stock.code} {stock.name}】等距下跌分析失败：{exc}")
        time.sleep(REQUEST_SLEEP_SECONDS)

    if processed_stock_count == 0:
        raise RuntimeError("所有符合市值条件的个股行情均获取失败")
    return ScreeningOutput(
        records=tuple(matched_records),
        warnings=tuple(warning_messages),
        latest_trade_date=required_trade_date,
    )


def run_stock_divergence_screening(
    kline_provider: CachedStockKlineProvider,
    stock_provider: StockProvider = get_eligible_stocks,
    calendar_fetcher: Callable[[], pd.DataFrame] = ak.tool_trade_date_hist_sina,
) -> ScreeningOutput:
    """遍历沪深大市值个股并输出日、周、月 MACD 底背离。"""
    warning_messages: list[str] = []
    stocks = stock_provider(warning_messages)
    if not stocks:
        raise RuntimeError("未获取到总市值大于 300 亿元的沪深 A 股")
    trade_calendar = calendar_fetcher()
    latest_trade_date = fetch_latest_trade_date(lambda: trade_calendar)
    start_date, end_date = get_divergence_date_range()
    logging.info(
        "开始执行个股 MACD 底背离筛选，共 %s 只股票，请求区间 %s 至 %s。",
        len(stocks),
        start_date,
        end_date,
    )

    matched_records: list[dict[str, object]] = []
    processed_stock_count = 0
    for index, stock in enumerate(stocks, start=1):
        logging.info(
            "[%s/%s] 正在筛选【%s %s】MACD 底背离。",
            index,
            len(stocks),
            stock.code,
            stock.name,
        )
        cached_result = kline_provider.load(stock, start_date, end_date, latest_trade_date)
        warning_messages.extend(cached_result.warnings)
        if cached_result.frame is None:
            time.sleep(REQUEST_SLEEP_SECONDS)
            continue
        processed_stock_count += 1
        try:
            timeframe_frames = aggregate_completed_timeframes(
                cached_result.frame,
                trade_calendar,
                latest_trade_date,
            )
            for timeframe, timeframe_frame in timeframe_frames.items():
                matched_record = analyze_macd_divergence(stock, timeframe, timeframe_frame)
                if matched_record is None:
                    continue
                matched_records.append(matched_record)
                logging.info(
                    "命中【%s %s】%s：%s，背离 %s 次。",
                    stock.code,
                    stock.name,
                    timeframe,
                    matched_record["背离分类"],
                    matched_record["背离次数"],
                )
        except Exception as exc:
            logging.exception("分析【%s %s】底背离失败，原因：%s", stock.code, stock.name, exc)
            warning_messages.append(f"【{stock.code} {stock.name}】底背离分析失败：{exc}")
        time.sleep(REQUEST_SLEEP_SECONDS)

    if processed_stock_count == 0:
        raise RuntimeError("所有符合市值条件的个股行情均获取失败")
    return ScreeningOutput(
        records=tuple(matched_records),
        warnings=tuple(warning_messages),
        latest_trade_date=latest_trade_date,
    )
