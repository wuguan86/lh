"""板块形态筛选的纯计算规则。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd


TARGET_TOLERANCE = 0.03
TARGET_PRICE_COLUMNS = (
    "1:1等距目标价",
    "1.272扩展目标价",
    "1.618扩展目标价",
)
TARGET_EXTENSION_RATIOS = (1.0, 1.272, 1.618)

OUTPUT_COLUMNS = [
    "板块类型",
    "板块名称",
    "最新交易日",
    "当前价格",
    "1:1等距目标价",
    "1.272扩展目标价",
    "1.618扩展目标价",
    "目标偏离率",
    "支撑位",
    "最高点价格",
    "上涨幅度",
    "首次跌破目标日期",
    "跌破目标后最低价",
    "最低价日期",
    "最大跌幅",
    "关联ETF代码",
    "关联ETF名称",
    "跌破日期",
    "统计天数",
    "下跌天数占比",
    "反弹天数",
    "20日乖离率",
    "最高点日期",
    "起涨点日期",
]


@dataclass(frozen=True)
class TargetDrawdown:
    """记录首次跌破目标价后出现的最深下跌。"""

    break_date: datetime
    lowest_price: float
    lowest_date: datetime
    decline_rate: float


def calculate_decline_target_prices(support_level: float, peak_price: float) -> tuple[float, ...]:
    """按上涨波段高度计算三档向下目标价。"""
    if support_level <= 0 or peak_price < support_level:
        raise ValueError("支撑位必须为正数且不能高于最高点")
    wave_height = peak_price - support_level
    return tuple(support_level - wave_height * ratio for ratio in TARGET_EXTENSION_RATIOS)


def normalize_target_price_fields(record: dict[str, object]) -> dict[str, object]:
    """兼容旧记录，并在波段价格完整时补算缺失的扩展目标。"""
    normalized_record = dict(record)
    if TARGET_PRICE_COLUMNS[0] not in normalized_record and "目标位价格" in normalized_record:
        normalized_record[TARGET_PRICE_COLUMNS[0]] = normalized_record["目标位价格"]

    has_all_target_prices = all(column in normalized_record for column in TARGET_PRICE_COLUMNS)
    if has_all_target_prices and "上涨幅度" in normalized_record:
        return normalized_record
    try:
        support_level = float(normalized_record["支撑位"])
        peak_price = float(normalized_record["最高点价格"])
        target_prices = calculate_decline_target_prices(
            support_level,
            peak_price,
        )
    except (KeyError, TypeError, ValueError):
        return normalized_record

    for column, target_price in zip(TARGET_PRICE_COLUMNS, target_prices):
        normalized_record.setdefault(column, round(target_price, 3) if target_price > 0 else "")
    normalized_record.setdefault("上涨幅度", f"{(peak_price - support_level) / support_level:.2%}")
    return normalized_record


def is_target_price_qualified(
    latest_close: float,
    target_price: float,
    tolerance: float = TARGET_TOLERANCE,
) -> bool:
    """目标价上方仅容忍配置比例，跌破目标价后不设置下限。"""
    if target_price <= 0:
        return False
    return latest_close <= target_price * (1 + tolerance)


def calculate_signed_target_deviation(latest_close: float, target_price: float) -> float:
    """计算带方向的目标偏离率，跌破目标价时返回正数。"""
    if target_price <= 0:
        raise ValueError("目标价格必须大于 0")
    return (target_price - latest_close) / target_price


def calculate_post_target_drawdown(
    kline_df: pd.DataFrame,
    target_price: float,
    start_position: int,
) -> TargetDrawdown | None:
    """从 A 点后寻找首次收盘跌破目标价，并统计此后的最低价。"""
    period_df = kline_df.iloc[start_position:]
    break_rows = period_df[period_df["close"] < target_price]
    if break_rows.empty:
        return None

    break_index = break_rows.index[0]
    post_break_df = kline_df.loc[break_index:]
    lowest_index = post_break_df["low"].idxmin()
    lowest_price = float(post_break_df.loc[lowest_index, "low"])

    return TargetDrawdown(
        break_date=pd.Timestamp(kline_df.loc[break_index, "date"]).to_pydatetime(),
        lowest_price=lowest_price,
        lowest_date=pd.Timestamp(kline_df.loc[lowest_index, "date"]).to_pydatetime(),
        decline_rate=(target_price - lowest_price) / target_price,
    )
