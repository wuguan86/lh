"""跨筛选、任务和网页层共享的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScreeningOutput:
    records: tuple[dict[str, object], ...]
    warnings: tuple[str, ...]
    latest_trade_date: str


def get_target_type(target: object) -> str:
    """读取板块或个股对象的业务类型。"""
    board_type = getattr(target, "board_type", None)
    if board_type is not None:
        return str(board_type)
    if hasattr(target, "code") and hasattr(target, "name"):
        return "个股"
    raise TypeError("筛选标的缺少类型信息")


def get_target_name(target: object) -> str:
    """读取板块或个股对象的展示名称。"""
    board_name = getattr(target, "board_name", None)
    if board_name is not None:
        return str(board_name)
    stock_name = getattr(target, "name", None)
    if stock_name is not None:
        return str(stock_name)
    raise TypeError("筛选标的缺少名称信息")


def build_target_identity_fields(target: object) -> dict[str, object]:
    """按标的类型生成稳定的板块或个股结果身份字段。"""
    target_type = get_target_type(target)
    target_name = get_target_name(target)
    if target_type == "个股":
        market_cap = float(getattr(target, "total_market_cap"))
        return {
            "股票代码": str(getattr(target, "code")),
            "股票名称": target_name,
            "总市值（亿元）": round(market_cap / 100_000_000, 2),
        }
    return {"板块类型": target_type, "板块名称": target_name}
