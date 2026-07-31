"""筛选结果 CSV 导出。"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Iterable

import pandas as pd

from board_screening.core import OUTPUT_COLUMNS


def records_to_csv_bytes(records: Iterable[dict[str, object]]) -> bytes:
    public_records = [
        {key: value for key, value in record.items() if not key.startswith("_")}
        for record in records
    ]
    result_df = pd.DataFrame(public_records, columns=OUTPUT_COLUMNS)
    buffer = StringIO()
    result_df.to_csv(buffer, index=False, lineterminator="\n")
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def write_latest_csv(records: Iterable[dict[str, object]], output_file: str | Path) -> None:
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(records_to_csv_bytes(records))

