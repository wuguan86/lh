"""MACD 多周期底背离纯计算规则。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from board_screening.market_data import BoardInfo


MACD_FAST_PERIOD = 12
MACD_SLOW_PERIOD = 26
MACD_SIGNAL_PERIOD = 9
MIN_PERIOD_ROWS = 95
SEARCH_WINDOW = 60
PIVOT_WINDOW = 2
RECENT_SIGNAL_BARS = 3
MIN_PIVOT_GAP = 5
MAX_PIVOT_GAP = 50
MIN_PRICE_DROP_RATE = 0.005
MIN_PRICE_REBOUND_RATE = 0.03
MIN_DIF_REBOUND_RATE = 0.20
MAX_GREEN_AREA_RATIO = 0.70

DIVERGENCE_OUTPUT_COLUMNS = [
    "筛选策略",
    "板块类型",
    "板块名称",
    "周期",
    "背离分类",
    "背离次数",
    "最新交易日",
    "当前价格",
    "第一低点日期",
    "第一低点价格",
    "第二低点日期",
    "第二低点价格",
    "价格创新低幅度",
    "价格反弹幅度",
    "第一DIF低点日期",
    "第一DIF低点",
    "第二DIF低点日期",
    "第二DIF低点",
    "DIF抬高幅度",
    "DIF回升幅度",
    "第一段绿柱面积",
    "第二段绿柱面积",
    "绿柱面积缩小率",
    "当前MACD柱",
    "当前柱状态",
    "低点链",
    "关联ETF代码",
    "关联ETF名称",
]


@dataclass(frozen=True)
class DivergencePivot:
    price_position: int
    price_date: pd.Timestamp
    price: float
    dif_position: int
    dif_date: pd.Timestamp
    dif: float


@dataclass(frozen=True)
class DivergenceEdge:
    start_index: int
    end_index: int
    price_rebound_rate: float
    dif_rebound_rate: float


@dataclass(frozen=True)
class DivergenceChain:
    pivot_indexes: tuple[int, ...]
    edges: tuple[DivergenceEdge, ...]


def calculate_macd(kline_frame: pd.DataFrame) -> pd.DataFrame:
    """按国内行情软件常用口径计算 DIF、DEA 和双倍柱值。"""
    calculated = kline_frame.copy()
    close_values = calculated["close"].astype(float)
    ema_fast = close_values.ewm(span=MACD_FAST_PERIOD, adjust=False).mean()
    ema_slow = close_values.ewm(span=MACD_SLOW_PERIOD, adjust=False).mean()
    calculated["dif"] = ema_fast - ema_slow
    calculated["dea"] = calculated["dif"].ewm(span=MACD_SIGNAL_PERIOD, adjust=False).mean()
    calculated["macd_histogram"] = 2 * (calculated["dif"] - calculated["dea"])
    return calculated


def find_divergence_pivots(calculated_frame: pd.DataFrame) -> list[DivergencePivot]:
    """在最近 60 根中寻找两侧各两根确认的价格低点及对应 DIF 低点。"""
    row_count = len(calculated_frame)
    search_start = max(PIVOT_WINDOW, row_count - SEARCH_WINDOW)
    search_end = row_count - PIVOT_WINDOW
    low_values = calculated_frame["low"].to_numpy(dtype=float)
    dif_values = calculated_frame["dif"].to_numpy(dtype=float)
    pivots: list[DivergencePivot] = []
    for position in range(search_start, search_end):
        current_low = low_values[position]
        left_values = low_values[position - PIVOT_WINDOW : position]
        right_values = low_values[position + 1 : position + PIVOT_WINDOW + 1]
        if not (current_low < np.min(left_values) and current_low < np.min(right_values)):
            continue
        dif_start = max(0, position - PIVOT_WINDOW)
        dif_end = min(row_count, position + PIVOT_WINDOW + 1)
        dif_position = dif_start + int(np.argmin(dif_values[dif_start:dif_end]))
        pivots.append(
            DivergencePivot(
                price_position=position,
                price_date=pd.Timestamp(calculated_frame.iloc[position]["date"]),
                price=current_low,
                dif_position=dif_position,
                dif_date=pd.Timestamp(calculated_frame.iloc[dif_position]["date"]),
                dif=float(dif_values[dif_position]),
            )
        )
    return pivots


def build_divergence_edges(
    calculated_frame: pd.DataFrame,
    pivots: list[DivergencePivot],
) -> list[DivergenceEdge]:
    """构建满足价格创新低、DIF 抬高及双重反弹条件的背离关系。"""
    high_values = calculated_frame["high"].to_numpy(dtype=float)
    low_values = calculated_frame["low"].to_numpy(dtype=float)
    dif_values = calculated_frame["dif"].to_numpy(dtype=float)
    edges: list[DivergenceEdge] = []
    for start_index, first_pivot in enumerate(pivots):
        for end_index in range(start_index + 1, len(pivots)):
            second_pivot = pivots[end_index]
            gap = second_pivot.price_position - first_pivot.price_position
            if gap < MIN_PIVOT_GAP:
                continue
            if gap > MAX_PIVOT_GAP:
                break
            if first_pivot.dif >= 0 or second_pivot.dif <= first_pivot.dif:
                continue
            if second_pivot.price > first_pivot.price * (1 - MIN_PRICE_DROP_RATE):
                continue

            # 后一个低点必须是该波段的新低，避免跨过更低的中间低点后产生伪背离。
            interim_lows = low_values[
                first_pivot.price_position + 1 : second_pivot.price_position
            ]
            if interim_lows.size and float(np.min(interim_lows)) <= second_pivot.price:
                continue

            interim_highs = high_values[
                first_pivot.price_position + 1 : second_pivot.price_position
            ]
            price_rebound_rate = (
                float(np.max(interim_highs)) - first_pivot.price
            ) / first_pivot.price
            if price_rebound_rate < MIN_PRICE_REBOUND_RATE:
                continue

            dif_start = first_pivot.dif_position + 1
            dif_end = second_pivot.dif_position
            if dif_end <= dif_start:
                continue
            interim_dif_max = float(np.max(dif_values[dif_start:dif_end]))
            dif_rebound = interim_dif_max - first_pivot.dif
            dif_rebound_rate = dif_rebound / abs(first_pivot.dif)
            if interim_dif_max <= max(first_pivot.dif, second_pivot.dif):
                continue
            if dif_rebound_rate < MIN_DIF_REBOUND_RATE:
                continue
            edges.append(
                DivergenceEdge(
                    start_index=start_index,
                    end_index=end_index,
                    price_rebound_rate=price_rebound_rate,
                    dif_rebound_rate=dif_rebound_rate,
                )
            )
    return edges


def select_latest_divergence_chain(
    pivots: list[DivergencePivot],
    edges: list[DivergenceEdge],
    row_count: int,
) -> DivergenceChain | None:
    """选择最新低点结束的最长背离链，并按起点和 DIF 抬升打破平局。"""
    recent_start = row_count - RECENT_SIGNAL_BARS
    recent_end_indexes = {
        index for index, pivot in enumerate(pivots) if pivot.price_position >= recent_start
    }
    if not recent_end_indexes:
        return None
    edges_by_end: dict[int, list[DivergenceEdge]] = {}
    for edge in edges:
        edges_by_end.setdefault(edge.end_index, []).append(edge)

    best_by_end: dict[int, DivergenceChain] = {}
    for end_index in range(len(pivots)):
        candidates: list[DivergenceChain] = []
        for edge in edges_by_end.get(end_index, []):
            preceding = best_by_end.get(edge.start_index)
            if preceding is None:
                candidates.append(
                    DivergenceChain((edge.start_index, edge.end_index), (edge,))
                )
            else:
                candidates.append(
                    DivergenceChain(
                        preceding.pivot_indexes + (edge.end_index,),
                        preceding.edges + (edge,),
                    )
                )
        if candidates:
            best_by_end[end_index] = max(
                candidates,
                key=lambda chain: _chain_priority(chain, pivots),
            )

    qualified_end_indexes = [index for index in recent_end_indexes if index in best_by_end]
    if not qualified_end_indexes:
        return None
    latest_end_index = max(
        qualified_end_indexes,
        key=lambda index: pivots[index].price_position,
    )
    return best_by_end[latest_end_index]


def _chain_priority(
    chain: DivergenceChain,
    pivots: list[DivergencePivot],
) -> tuple[int, int, float]:
    first_pivot = pivots[chain.pivot_indexes[0]]
    last_pivot = pivots[chain.pivot_indexes[-1]]
    return len(chain.edges), first_pivot.price_position, last_pivot.dif - first_pivot.dif


def calculate_completed_green_area(
    histogram_values: np.ndarray,
    position: int,
) -> tuple[int, int, float] | None:
    """计算指定位置所在的完整连续绿柱区间，未结束的绿柱段不参与比较。"""
    if position < 0 or position >= len(histogram_values) or histogram_values[position] >= 0:
        return None
    start = position
    while start > 0 and histogram_values[start - 1] < 0:
        start -= 1
    end = position
    while end + 1 < len(histogram_values) and histogram_values[end + 1] < 0:
        end += 1
    if end == len(histogram_values) - 1:
        return None
    return start, end, float(np.abs(histogram_values[start : end + 1]).sum())


def analyze_macd_divergence(
    board: BoardInfo,
    timeframe: str,
    kline_frame: pd.DataFrame,
) -> dict[str, object] | None:
    """分析单个板块单一周期，命中时返回可存储和导出的诊断记录。"""
    if len(kline_frame) < MIN_PERIOD_ROWS:
        return None
    calculated = calculate_macd(kline_frame.reset_index(drop=True))
    pivots = find_divergence_pivots(calculated)
    edges = build_divergence_edges(calculated, pivots)
    chain = select_latest_divergence_chain(pivots, edges, len(calculated))
    if chain is None:
        return None

    first_pivot = pivots[chain.pivot_indexes[0]]
    last_pivot = pivots[chain.pivot_indexes[-1]]
    last_edge = chain.edges[-1]
    histogram_values = calculated["macd_histogram"].to_numpy(dtype=float)
    first_green_area = calculate_completed_green_area(histogram_values, first_pivot.dif_position)
    second_green_area = calculate_completed_green_area(histogram_values, last_pivot.dif_position)
    green_area_ratio: float | None = None
    has_green_area_divergence = False
    if first_green_area and second_green_area and first_green_area[:2] != second_green_area[:2]:
        green_area_ratio = second_green_area[2] / first_green_area[2]
        has_green_area_divergence = green_area_ratio <= MAX_GREEN_AREA_RATIO

    current_histogram = float(histogram_values[-1])
    has_red_histogram = current_histogram > 0
    divergence_count = len(chain.edges)
    labels: list[str] = []
    if has_green_area_divergence:
        labels.append("线和绿柱双背离")
    if has_red_histogram:
        labels.append("背离+红柱")
    if divergence_count >= 2:
        labels.append("多次背离")
    if not labels:
        labels.append("单纯底背离")

    price_drop_rate = (first_pivot.price - last_pivot.price) / first_pivot.price
    dif_lift_rate = (last_pivot.dif - first_pivot.dif) / abs(first_pivot.dif)
    latest_row = calculated.iloc[-1]
    first_area_value = first_green_area[2] if first_green_area else None
    second_area_value = second_green_area[2] if second_green_area else None
    point_chain = " | ".join(
        f"{pivots[index].price_date:%Y-%m-%d}({pivots[index].price:.3f}/{pivots[index].dif:.4f})"
        for index in chain.pivot_indexes
    )
    return {
        "筛选策略": "MACD底背离",
        "板块类型": board.board_type,
        "板块名称": board.board_name,
        "周期": timeframe,
        "背离分类": "、".join(labels),
        "背离次数": divergence_count,
        "最新交易日": pd.Timestamp(latest_row["date"]).strftime("%Y-%m-%d"),
        "当前价格": round(float(latest_row["close"]), 3),
        "第一低点日期": first_pivot.price_date.strftime("%Y-%m-%d"),
        "第一低点价格": round(first_pivot.price, 3),
        "第二低点日期": last_pivot.price_date.strftime("%Y-%m-%d"),
        "第二低点价格": round(last_pivot.price, 3),
        "价格创新低幅度": f"{price_drop_rate:.2%}",
        "价格反弹幅度": f"{last_edge.price_rebound_rate:.2%}",
        "第一DIF低点日期": first_pivot.dif_date.strftime("%Y-%m-%d"),
        "第一DIF低点": round(first_pivot.dif, 6),
        "第二DIF低点日期": last_pivot.dif_date.strftime("%Y-%m-%d"),
        "第二DIF低点": round(last_pivot.dif, 6),
        "DIF抬高幅度": f"{dif_lift_rate:.2%}",
        "DIF回升幅度": f"{last_edge.dif_rebound_rate:.2%}",
        "第一段绿柱面积": round(first_area_value, 6) if first_area_value is not None else "",
        "第二段绿柱面积": round(second_area_value, 6) if second_area_value is not None else "",
        "绿柱面积缩小率": f"{1 - green_area_ratio:.2%}" if green_area_ratio is not None else "",
        "当前MACD柱": round(current_histogram, 6),
        "当前柱状态": "红柱" if has_red_histogram else "绿柱",
        "低点链": point_chain,
        "关联ETF代码": "",
        "关联ETF名称": "",
    }
