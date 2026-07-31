"""
A 股同花顺板块等距下跌形态筛选程序。

运行方式：
    python board_pattern_screener.py
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

import akshare as ak
import numpy as np
import pandas as pd

from board_screening.core import (
    OUTPUT_COLUMNS,
    calculate_post_target_drawdown,
    calculate_signed_target_deviation,
    is_target_price_qualified,
)
from board_screening.enrichment import DataEnricher
from board_screening.models import ScreeningOutput


LOOKBACK_TRADING_DAYS = 90
SWING_WINDOW = 5
MIN_BREAK_PERIOD_DAYS = 5
CLOSE_DOWN_RATIO_THRESHOLD = 0.60
MAX_REBOUND_DAYS = 1
BIAS_MA_WINDOW = 20
BIAS_THRESHOLD = 0.07
FETCH_CALENDAR_DAYS = 220
RETRY_TIMES = 3
REQUEST_SLEEP_SECONDS = 0.8
OUTPUT_FILE = "ths_board_screen_result.csv"

BOARD_TYPE_INDUSTRY = "行业"
BOARD_TYPE_CONCEPT = "概念"

@dataclass(frozen=True)
class BoardInfo:
    board_type: str
    board_name: str


def setup_logging() -> None:
    """配置中文日志格式，便于观察网络异常、跳过原因和保存结果。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def get_all_boards(warnings: list[str] | None = None) -> list[BoardInfo]:
    """获取同花顺行业和概念板块列表，任一列表失败时记录日志并继续处理另一类。"""
    board_frames: list[pd.DataFrame] = []
    warning_messages = warnings if warnings is not None else []

    board_sources: list[tuple[str, Callable[[], pd.DataFrame]]] = [
        (BOARD_TYPE_INDUSTRY, ak.stock_board_industry_name_ths),
        (BOARD_TYPE_CONCEPT, ak.stock_board_concept_name_ths),
    ]

    for board_type, fetcher in board_sources:
        try:
            raw_df = fetcher()
            if raw_df.empty or "name" not in raw_df.columns:
                message = f"获取{board_type}板块列表为空或缺少 name 字段，已跳过。"
                logging.warning(message)
                warning_messages.append(message)
                continue

            temp_df = raw_df[["name"]].copy()
            temp_df["board_type"] = board_type
            temp_df.rename(columns={"name": "board_name"}, inplace=True)
            board_frames.append(temp_df)
            logging.info("已获取%s板块列表，共 %s 个。", board_type, len(temp_df))
        except Exception as exc:
            logging.exception("获取%s板块列表失败，原因：%s", board_type, exc)
            warning_messages.append(f"获取{board_type}板块列表失败：{exc}")

    if not board_frames:
        return []

    boards_df = pd.concat(board_frames, ignore_index=True)
    boards_df["board_name"] = boards_df["board_name"].astype(str).str.strip()
    boards_df = boards_df[boards_df["board_name"] != ""]
    boards_df.drop_duplicates(subset=["board_type", "board_name"], inplace=True)

    return [
        BoardInfo(board_type=row.board_type, board_name=row.board_name)
        for row in boards_df.itertuples(index=False)
    ]


def get_date_range() -> tuple[str, str]:
    """按自然日回溯一段时间，确保清洗后通常仍有足够的 90 个交易日。"""
    end_day = date.today()
    start_day = end_day - timedelta(days=FETCH_CALENDAR_DAYS)
    return start_day.strftime("%Y%m%d"), end_day.strftime("%Y%m%d")


def fetch_board_kline(board: BoardInfo, start_date: str, end_date: str) -> pd.DataFrame:
    """按板块类型调用对应 AKShare 接口获取同花顺日 K 数据。"""
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
) -> pd.DataFrame | None:
    """网络数据可能偶发失败，单板块重试后仍失败则返回 None，避免中断全局筛选。"""
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            return fetch_board_kline(board, start_date, end_date)
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
                # 失败后逐步拉长等待时间，降低源站临时限流对后续请求的影响。
                time.sleep(REQUEST_SLEEP_SECONDS * attempt)

    logging.error("【%s-%s】连续获取失败，已跳过。", board.board_type, board.board_name)
    return None


def normalize_kline(raw_df: pd.DataFrame) -> pd.DataFrame:
    """统一中文行情字段为英文内部字段，并完成日期排序和数值清洗。"""
    column_map = {
        "日期": "date",
        "开盘价": "open",
        "最高价": "high",
        "最低价": "low",
        "收盘价": "close",
        "成交量": "volume",
        "成交额": "amount",
    }
    required_columns = list(column_map.keys())
    missing_columns = [column for column in required_columns if column not in raw_df.columns]
    if missing_columns:
        raise ValueError(f"日 K 数据缺少必要字段：{missing_columns}")

    kline_df = raw_df[required_columns].rename(columns=column_map).copy()
    kline_df["date"] = pd.to_datetime(kline_df["date"], errors="coerce")

    numeric_columns = ["open", "high", "low", "close", "volume", "amount"]
    for column in numeric_columns:
        kline_df[column] = pd.to_numeric(kline_df[column], errors="coerce")

    # 核心形态只依赖日期、高低收；这些字段缺失时该交易日无法参与计算。
    kline_df.dropna(subset=["date", "high", "low", "close"], inplace=True)
    kline_df.sort_values("date", inplace=True)
    kline_df.drop_duplicates(subset=["date"], keep="last", inplace=True)
    kline_df.reset_index(drop=True, inplace=True)
    return kline_df


def find_nearest_left_local_low(
    lookback_df: pd.DataFrame,
    peak_position: int,
) -> int | None:
    """从最高点向左寻找最近波谷，波谷需严格低于前后各 5 天最低价。"""
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
            return position

    return None


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


def analyze_board_pattern(board: BoardInfo, kline_df: pd.DataFrame) -> dict[str, object] | None:
    """计算等距下跌形态，满足跌破支撑、下跌流畅和乖离率条件时返回结果记录。"""
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

    support_position = find_nearest_left_local_low(lookback_df, peak_position)
    if support_position is None:
        logging.info(
            "【%s-%s】最高点左侧未找到符合定义的局部低点，已跳过。",
            board.board_type,
            board.board_name,
        )
        return None

    support_level = float(lookback_df.loc[support_position, "low"])
    wave_height = peak_price - support_level
    target_price = support_level - wave_height
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
        "目标位价格": round(target_price, 3),
        "目标偏离率": f"{target_deviation:.2%}",
        "支撑位": round(support_level, 3),
        "最高点价格": round(peak_price, 3),
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
        "市值龙头1": "",
        "市值龙头2": "",
        "市值龙头3": "",
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


def run_screening(enricher: DataEnricher | None = None) -> ScreeningOutput:
    """执行完整筛选并仅为命中板块补充 ETF 和市值龙头。"""
    start_date, end_date = get_date_range()
    warning_messages: list[str] = []
    boards = get_all_boards(warning_messages)
    if not boards:
        raise RuntimeError("未获取到任何同花顺板块")

    print(
        f"开始筛选同花顺行业 + 概念板块，共 {len(boards)} 个；"
        f"请求区间：{start_date} 至 {end_date}。"
    )

    matched_records: list[dict[str, object]] = []
    latest_trade_dates: list[str] = []
    processed_board_count = 0
    for index, board in enumerate(boards, start=1):
        print(f"[{index}/{len(boards)}] 正在筛选【{board.board_type}】{board.board_name} ...")

        raw_df = fetch_board_kline_with_retry(board, start_date, end_date)
        if raw_df is None:
            warning_messages.append(f"【{board.board_type}-{board.board_name}】日 K 数据获取失败")
            time.sleep(REQUEST_SLEEP_SECONDS)
            continue

        try:
            kline_df = normalize_kline(raw_df)
            if not kline_df.empty:
                latest_trade_dates.append(kline_df.iloc[-1]["date"].strftime("%Y-%m-%d"))
                processed_board_count += 1
            matched_record = analyze_board_pattern(board, kline_df)
        except Exception as exc:
            logging.exception("分析【%s-%s】失败，原因：%s", board.board_type, board.board_name, exc)
            warning_messages.append(f"【{board.board_type}-{board.board_name}】分析失败：{exc}")
            time.sleep(REQUEST_SLEEP_SECONDS)
            continue

        if matched_record is not None:
            matched_records.append(matched_record)
            print(
                f"    命中：当前价 {matched_record['当前价格']}，"
                f"目标位 {matched_record['目标位价格']}，"
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


def main() -> None:
    setup_logging()
    try:
        output = run_screening()
    except Exception as exc:
        logging.exception("筛选任务失败，原因：%s", exc)
        print(f"\n筛选任务失败：{exc}")
        save_results(pd.DataFrame(columns=OUTPUT_COLUMNS))
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
    main()
