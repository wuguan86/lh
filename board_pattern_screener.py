"""
A 股同花顺板块等距下跌形态筛选程序。

运行方式：
    python board_pattern_screener.py
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import time

import akshare as ak
import numpy as np
import pandas as pd

from board_screening.core import (
    OUTPUT_COLUMNS,
    calculate_decline_target_prices,
    calculate_post_target_drawdown,
    calculate_signed_target_deviation,
    is_target_price_qualified,
)
from board_screening.enrichment import DataEnricher
from board_screening.market_data import (
    BOARD_TYPE_CONCEPT,
    BOARD_TYPE_INDUSTRY,
    REQUEST_SLEEP_SECONDS,
    BoardInfo,
    CachedKlineProvider,
    KlineCache,
    fetch_board_kline,
    fetch_board_kline_with_retry,
    get_all_boards,
    get_date_range,
    normalize_kline,
)
from board_screening.models import ScreeningOutput
from board_screening.scheduler import fetch_latest_trade_date
from board_screening.strategies import STRATEGY_EQUAL_DECLINE, STRATEGY_MACD_DIVERGENCE


LOOKBACK_TRADING_DAYS = 90
SWING_WINDOW = 5
MIN_BREAK_PERIOD_DAYS = 5
CLOSE_DOWN_RATIO_THRESHOLD = 0.60
MAX_REBOUND_DAYS = 1
BIAS_MA_WINDOW = 20
BIAS_THRESHOLD = 0.07
OUTPUT_FILE = "ths_board_screen_result.csv"
DEFAULT_MIN_WAVE_RISE_PERCENT = 10.0


def setup_logging() -> None:
    """配置中文日志格式，便于观察网络异常、跳过原因和保存结果。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def find_nearest_left_local_low(
    lookback_df: pd.DataFrame,
    peak_position: int,
    peak_price: float | None = None,
    min_wave_rise_rate: float = 0,
) -> int | None:
    """从最高点向左寻找最近的合格波谷，小波段不合格时继续寻找更大波段。"""
    last_valid_position = len(lookback_df) - SWING_WINDOW - 1
    start_position = min(peak_position - 1, last_valid_position)

    if start_position < SWING_WINDOW:
        return None

    low_values = lookback_df["low"].to_numpy(dtype=float)
    for position in range(start_position, SWING_WINDOW - 1, -1):
        current_low = low_values[position]
        previous_min = np.nanmin(low_values[position - SWING_WINDOW : position])
        next_min = np.nanmin(low_values[position + 1 : position + SWING_WINDOW + 1])

        if current_low < previous_min and current_low < next_min:
            if peak_price is None:
                return position
            wave_rise_rate = (peak_price - current_low) / current_low if current_low > 0 else -1
            if wave_rise_rate >= min_wave_rise_rate:
                return position

    return None


def get_min_wave_rise_rate() -> float:
    """读取最小上涨幅度百分比，默认 10，允许用环境变量调整。"""
    raw_value = os.getenv("MIN_WAVE_RISE_PERCENT", str(DEFAULT_MIN_WAVE_RISE_PERCENT))
    try:
        percent = float(raw_value)
    except ValueError as exc:
        raise ValueError("MIN_WAVE_RISE_PERCENT 必须是非负数字") from exc
    if not math.isfinite(percent) or percent < 0:
        raise ValueError("MIN_WAVE_RISE_PERCENT 必须是非负数字")
    return percent / 100


def find_first_break_support_position(
    lookback_df: pd.DataFrame,
    support_level: float,
    peak_position: int,
) -> int | None:
    """从最高点之后寻找首次收盘跌破支撑位的交易日，作为 A 点统计起点。"""
    close_values = lookback_df["close"].to_numpy(dtype=float)
    for position in range(peak_position + 1, len(lookback_df)):
        if close_values[position] < support_level:
            return position

    return None


def calculate_break_period_metrics(
    lookback_df: pd.DataFrame,
    break_position: int,
) -> tuple[int, int, float, int]:
    """统计 A 点到最新价区间内的下跌流畅度和弱反弹次数。"""
    period_df = lookback_df.iloc[break_position:].copy()
    previous_close = lookback_df["close"].shift(1).iloc[break_position:]
    previous_high = lookback_df["high"].shift(1).iloc[break_position:]

    period_days = len(period_df)
    # 下跌天数按“今天收盘价低于昨天收盘价”计算，A 点也会和前一交易日比较。
    close_down_days = int((period_df["close"] < previous_close).sum())
    close_down_ratio = close_down_days / period_days
    # 只容忍极弱抵抗：收盘价重新站上前一日最高价的次数不能超过配置上限。
    rebound_days = int((period_df["close"] > previous_high).sum())

    return period_days, close_down_days, close_down_ratio, rebound_days


def calculate_latest_bias(kline_df: pd.DataFrame) -> float | None:
    """计算最新收盘价相对 20 日均线的向下乖离率。"""
    if len(kline_df) < BIAS_MA_WINDOW:
        return None

    latest_close = float(kline_df.iloc[-1]["close"])
    moving_average = float(kline_df["close"].tail(BIAS_MA_WINDOW).mean())
    if moving_average <= 0:
        return None

    return (moving_average - latest_close) / moving_average


def analyze_board_pattern(
    board: BoardInfo,
    kline_df: pd.DataFrame,
    min_wave_rise_rate: float = DEFAULT_MIN_WAVE_RISE_PERCENT / 100,
) -> dict[str, object] | None:
    """计算等距下跌形态，满足跌破支撑、下跌流畅和乖离率条件时返回结果记录。"""
    if not math.isfinite(min_wave_rise_rate) or min_wave_rise_rate < 0:
        raise ValueError("最小上涨幅度必须是非负数")
    if len(kline_df) < LOOKBACK_TRADING_DAYS:
        logging.info(
            "【%s-%s】数据不足 %s 个交易日，当前仅 %s 条，已跳过。",
            board.board_type,
            board.board_name,
            LOOKBACK_TRADING_DAYS,
            len(kline_df),
        )
        return None

    lookback_df = kline_df.tail(LOOKBACK_TRADING_DAYS).reset_index(drop=True)
    peak_price = float(lookback_df["high"].max())

    # 若最高价多次出现，选择最近一次，更贴合“近期最高点”的业务语义。
    peak_candidates = np.flatnonzero(np.isclose(lookback_df["high"].to_numpy(dtype=float), peak_price))
    peak_position = int(peak_candidates[-1])
    if peak_position <= SWING_WINDOW:
        logging.info(
            "【%s-%s】最高点左侧样本不足，无法寻找前 5 天完整波谷，已跳过。",
            board.board_type,
            board.board_name,
        )
        return None

    support_position = find_nearest_left_local_low(
        lookback_df,
        peak_position,
        peak_price,
        min_wave_rise_rate,
    )
    if support_position is None:
        logging.info(
            "【%s-%s】最高点左侧未找到上涨幅度达到 %.2f%% 的局部低点，已跳过。",
            board.board_type,
            board.board_name,
            min_wave_rise_rate * 100,
        )
        return None

    support_level = float(lookback_df.loc[support_position, "low"])
    wave_height = peak_price - support_level
    wave_rise_rate = wave_height / support_level
    target_price, extension_target_price_1_272, extension_target_price_1_618 = (
        calculate_decline_target_prices(support_level, peak_price)
    )
    latest_row = lookback_df.iloc[-1]
    latest_close = float(latest_row["close"])

    if target_price <= 0:
        logging.info(
            "【%s-%s】目标位为非正数 %.3f，不适合按比例计算偏离率，已跳过。",
            board.board_type,
            board.board_name,
            target_price,
        )
        return None

    is_break_support = latest_close < support_level
    target_deviation = calculate_signed_target_deviation(latest_close, target_price)
    is_near_target = is_target_price_qualified(latest_close, target_price)

    if not (is_break_support and is_near_target):
        return None

    break_position = find_first_break_support_position(lookback_df, support_level, peak_position)
    if break_position is None:
        return None

    period_days, close_down_days, close_down_ratio, rebound_days = calculate_break_period_metrics(
        lookback_df,
        break_position,
    )
    if period_days < MIN_BREAK_PERIOD_DAYS:
        return None

    is_smooth_decline = close_down_ratio > CLOSE_DOWN_RATIO_THRESHOLD
    is_rebound_tolerable = rebound_days <= MAX_REBOUND_DAYS
    latest_bias = calculate_latest_bias(kline_df)
    is_bias_qualified = latest_bias is not None and latest_bias > BIAS_THRESHOLD

    if not (is_smooth_decline and is_rebound_tolerable and is_bias_qualified):
        return None

    target_drawdown = calculate_post_target_drawdown(lookback_df, target_price, break_position)

    return {
        "板块类型": board.board_type,
        "板块名称": board.board_name,
        "最新交易日": latest_row["date"].strftime("%Y-%m-%d"),
        "当前价格": round(latest_close, 3),
        "1:1等距目标价": round(target_price, 3),
        "1.272扩展目标价": (
            round(extension_target_price_1_272, 3) if extension_target_price_1_272 > 0 else ""
        ),
        "1.618扩展目标价": (
            round(extension_target_price_1_618, 3) if extension_target_price_1_618 > 0 else ""
        ),
        "目标偏离率": f"{target_deviation:.2%}",
        "支撑位": round(support_level, 3),
        "最高点价格": round(peak_price, 3),
        "上涨幅度": f"{wave_rise_rate:.2%}",
        "首次跌破目标日期": (
            target_drawdown.break_date.strftime("%Y-%m-%d") if target_drawdown else ""
        ),
        "跌破目标后最低价": round(target_drawdown.lowest_price, 3) if target_drawdown else "",
        "最低价日期": (
            target_drawdown.lowest_date.strftime("%Y-%m-%d") if target_drawdown else ""
        ),
        "最大跌幅": f"{target_drawdown.decline_rate:.2%}" if target_drawdown else "",
        "关联ETF代码": "",
        "关联ETF名称": "",
        "跌破日期": lookback_df.loc[break_position, "date"].strftime("%Y-%m-%d"),
        "统计天数": period_days,
        "下跌天数占比": f"{close_down_ratio:.2%}",
        "反弹天数": rebound_days,
        "20日乖离率": f"{latest_bias:.2%}",
        "最高点日期": lookback_df.loc[peak_position, "date"].strftime("%Y-%m-%d"),
        "起涨点日期": lookback_df.loc[support_position, "date"].strftime("%Y-%m-%d"),
    }


def save_results(result_df: pd.DataFrame) -> None:
    """使用 UTF-8 BOM 保存 CSV，方便 Windows Excel 直接打开中文结果。"""
    result_df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
    logging.info("筛选结果已保存到：%s", OUTPUT_FILE)


def run_screening(
    enricher: DataEnricher | None = None,
    min_wave_rise_rate: float | None = None,
    kline_provider: CachedKlineProvider | None = None,
    required_trade_date: str | None = None,
) -> ScreeningOutput:
    """执行完整筛选并仅为命中板块补充关联 ETF。"""
    configured_min_wave_rise_rate = (
        get_min_wave_rise_rate() if min_wave_rise_rate is None else min_wave_rise_rate
    )
    if not math.isfinite(configured_min_wave_rise_rate) or configured_min_wave_rise_rate < 0:
        raise ValueError("最小上涨幅度必须是非负数")
    start_date, end_date = get_date_range()
    warning_messages: list[str] = []
    boards = get_all_boards(warning_messages)
    if not boards:
        raise RuntimeError("未获取到任何同花顺板块")

    print(
        f"开始筛选同花顺行业 + 概念板块，共 {len(boards)} 个；"
        f"请求区间：{start_date} 至 {end_date}；"
        f"最小上涨幅度：{configured_min_wave_rise_rate:.2%}。"
    )

    matched_records: list[dict[str, object]] = []
    latest_trade_dates: list[str] = []
    processed_board_count = 0
    for index, board in enumerate(boards, start=1):
        print(f"[{index}/{len(boards)}] 正在筛选【{board.board_type}】{board.board_name} ...")

        try:
            if kline_provider is None:
                raw_df = fetch_board_kline_with_retry(board, start_date, end_date)
                if raw_df is None:
                    warning_messages.append(f"【{board.board_type}-{board.board_name}】日 K 数据获取失败")
                    time.sleep(REQUEST_SLEEP_SECONDS)
                    continue
                kline_df = normalize_kline(raw_df)
            else:
                cached_result = kline_provider.load(
                    board,
                    start_date,
                    end_date,
                    required_trade_date or end_date,
                )
                warning_messages.extend(cached_result.warnings)
                if cached_result.frame is None:
                    continue
                kline_df = cached_result.frame
            if not kline_df.empty:
                latest_trade_dates.append(kline_df.iloc[-1]["date"].strftime("%Y-%m-%d"))
                processed_board_count += 1
            matched_record = analyze_board_pattern(
                board,
                kline_df,
                configured_min_wave_rise_rate,
            )
        except Exception as exc:
            logging.exception("分析【%s-%s】失败，原因：%s", board.board_type, board.board_name, exc)
            warning_messages.append(f"【{board.board_type}-{board.board_name}】分析失败：{exc}")
            time.sleep(REQUEST_SLEEP_SECONDS)
            continue

        if matched_record is not None:
            matched_records.append(matched_record)
            print(
                f"    命中：当前价 {matched_record['当前价格']}，"
                f"1:1 等距目标位 {matched_record['1:1等距目标价']}，"
                f"偏离率 {matched_record['目标偏离率']}。"
            )

        # 正常请求之间也保留固定延时，避免连续遍历时对源站造成过高压力。
        time.sleep(REQUEST_SLEEP_SECONDS)

    if processed_board_count == 0 or not latest_trade_dates:
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
        latest_trade_date=max(latest_trade_dates),
    )


def main(strategy: str = STRATEGY_EQUAL_DECLINE) -> None:
    setup_logging()
    database_path = os.getenv("DATABASE_PATH", "data/screening.db")
    kline_cache = KlineCache(database_path)
    kline_cache.initialize()
    kline_provider = CachedKlineProvider(kline_cache)
    try:
        if strategy == STRATEGY_MACD_DIVERGENCE:
            from board_screening.divergence_screening import run_divergence_screening

            output = run_divergence_screening(kline_provider)
        else:
            output = run_screening(
                kline_provider=kline_provider,
                required_trade_date=fetch_latest_trade_date(),
            )
    except Exception as exc:
        logging.exception("筛选任务失败，原因：%s", exc)
        print(f"\n筛选任务失败：{exc}")
        if strategy == STRATEGY_EQUAL_DECLINE:
            save_results(pd.DataFrame(columns=OUTPUT_COLUMNS))
        return

    if strategy == STRATEGY_MACD_DIVERGENCE:
        from board_screening.export import write_latest_csv
        from board_screening.macd_divergence import DIVERGENCE_OUTPUT_COLUMNS

        result_df = pd.DataFrame(output.records, columns=DIVERGENCE_OUTPUT_COLUMNS)
        output_file = os.getenv(
            "DIVERGENCE_OUTPUT_FILE",
            "ths_board_macd_divergence_result.csv",
        )
        print("\n========== MACD 底背离筛选结果 ==========")
        print(result_df.to_string(index=False) if not result_df.empty else "本次未筛选到底背离板块。")
        write_latest_csv(output.records, output_file, STRATEGY_MACD_DIVERGENCE)
        logging.info("筛选结果已保存到：%s", output_file)
        return

    result_df = pd.DataFrame(output.records, columns=OUTPUT_COLUMNS)
    if not result_df.empty:
        deviation_order = result_df["目标偏离率"].str.rstrip("%").astype(float).abs()
        result_df = result_df.assign(_deviation_order=deviation_order)
        result_df.sort_values(["板块类型", "_deviation_order", "板块名称"], inplace=True)
        result_df.drop(columns=["_deviation_order"], inplace=True)
        result_df.reset_index(drop=True, inplace=True)

    print("\n========== 最终筛选结果 ==========")
    if result_df.empty:
        print("本次未筛选到符合等距下跌目标位条件的板块。")
    else:
        print(result_df.to_string(index=False))

    save_results(result_df)


if __name__ == "__main__":
    argument_parser = argparse.ArgumentParser(description="同花顺板块形态筛选")
    argument_parser.add_argument(
        "--strategy",
        choices=("equal-decline", "macd-divergence"),
        default="equal-decline",
        help="筛选策略，默认执行等距下跌",
    )
    arguments = argument_parser.parse_args()
    strategy_mapping = {
        "equal-decline": STRATEGY_EQUAL_DECLINE,
        "macd-divergence": STRATEGY_MACD_DIVERGENCE,
    }
    main(strategy_mapping[arguments.strategy])
