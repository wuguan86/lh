"""跨筛选、任务和网页层共享的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScreeningOutput:
    records: tuple[dict[str, object], ...]
    warnings: tuple[str, ...]
    latest_trade_date: str

