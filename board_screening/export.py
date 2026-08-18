"""筛选结果 CSV 导出。"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Iterable

import pandas as pd

from board_screening.core import OUTPUT_COLUMNS, normalize_target_price_fields
from board_screening.macd_divergence import DIVERGENCE_OUTPUT_COLUMNS
from board_screening.strategies import STRATEGY_EQUAL_DECLINE, UNIVERSE_BOARD, UNIVERSE_STOCK


STOCK_EQUAL_DECLINE_COLUMNS = [
    "股票代码",
    "股票名称",
    "总市值（亿元）",
    *[column for column in OUTPUT_COLUMNS if column not in {"板块类型", "板块名称", "关联ETF代码", "关联ETF名称"}],
]
STOCK_DIVERGENCE_COLUMNS = [
    "股票代码",
    "股票名称",
    "总市值（亿元）",
    *[
        column
        for column in DIVERGENCE_OUTPUT_COLUMNS
        if column not in {"板块类型", "板块名称", "关联ETF代码", "关联ETF名称"}
    ],
]


def records_to_csv_bytes(
    records: Iterable[dict[str, object]],
    strategy: str = STRATEGY_EQUAL_DECLINE,
    universe: str = UNIVERSE_BOARD,
) -> bytes:
    public_records = []
    for record in records:
        public_record = {key: value for key, value in record.items() if not key.startswith("_")}
        if strategy == STRATEGY_EQUAL_DECLINE:
            public_record = normalize_target_price_fields(public_record)
        public_records.append(public_record)
    if universe == UNIVERSE_STOCK:
        columns = (
            STOCK_EQUAL_DECLINE_COLUMNS
            if strategy == STRATEGY_EQUAL_DECLINE
            else STOCK_DIVERGENCE_COLUMNS
        )
    else:
        columns = OUTPUT_COLUMNS if strategy == STRATEGY_EQUAL_DECLINE else DIVERGENCE_OUTPUT_COLUMNS
    result_df = pd.DataFrame(public_records, columns=columns)
    buffer = StringIO()
    result_df.to_csv(buffer, index=False, lineterminator="\n")
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def write_latest_csv(
    records: Iterable[dict[str, object]],
    output_file: str | Path,
    strategy: str = STRATEGY_EQUAL_DECLINE,
    universe: str = UNIVERSE_BOARD,
) -> None:
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(records_to_csv_bytes(records, strategy, universe))
