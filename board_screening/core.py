"""板块形态筛选的纯计算规则。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd


TARGET_TOLERANCE = 0.03

OUTPUT_COLUMNS = [
    "板块类型",
    "板块名称",
    "最新交易日",
    "当前价格",
    "目标位价格",
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
    "市值龙头1",
    "市值龙头2",
    "市值龙头3",
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
