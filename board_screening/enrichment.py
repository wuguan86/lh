"""ETF 名称匹配与板块龙头筛选。"""

from __future__ import annotations

import re
import logging
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable

import akshare as ak
import pandas as pd


GENERIC_THEME_WORDS = (
    "交易型开放式",
    "发起式",
    "概念",
    "主题",
    "指数",
    "ETF",
    "联接",
    "中证",
    "上证",
    "深证",
)


@dataclass(frozen=True)
class EtfMatch:
    code: str
    name: str
    score: float


@dataclass(frozen=True)
class StockLeader:
    code: str
    name: str
    market_cap: float


@dataclass(frozen=True)
class EnrichmentOutcome:
    record: dict[str, object]
    warnings: tuple[str, ...]


def normalize_theme_name(value: object) -> str:
    """清理主题名称中的通用词和符号，保留可比较的核心文字。"""
    normalized = str(value).upper()
    for word in GENERIC_THEME_WORDS:
        normalized = normalized.replace(word, "")
    return re.sub(r"[^0-9A-Z\u4e00-\u9fff]", "", normalized)


def _extract_board_terms(board_name: str) -> list[str]:
    parenthetical_terms = re.findall(r"[（(]([^）)]+)[）)]", board_name)
    outside_name = re.sub(r"[（(][^）)]+[）)]", "", board_name)
    terms = [normalize_theme_name(outside_name)]
    terms.extend(normalize_theme_name(term) for term in parenthetical_terms)
    return [term for term in terms if len(term) >= 2]


def _theme_score(board_terms: list[str], etf_name: str) -> tuple[float, int]:
    normalized_etf_name = normalize_theme_name(etf_name)
    best_score = 0.0
    best_match_length = 0
    for term in board_terms:
        match = SequenceMatcher(None, term, normalized_etf_name).find_longest_match()
        score = match.size / len(term)
        if (score, match.size) > (best_score, best_match_length):
            best_score = score
            best_match_length = match.size
    return best_score, best_match_length


def match_related_etf(board_name: str, etf_df: pd.DataFrame) -> EtfMatch | None:
    """按主题名称选择最贴近板块的 ETF，低可信匹配直接放弃。"""
    required_columns = {"基金代码", "基金名称"}
    if etf_df.empty or not required_columns.issubset(etf_df.columns):
        return None

    board_terms = _extract_board_terms(board_name)
    candidates: list[tuple[float, int, int, str, str]] = []
    for row in etf_df.itertuples(index=False):
        code = str(getattr(row, "基金代码")).zfill(6)
        name = str(getattr(row, "基金名称"))
        score, match_length = _theme_score(board_terms, name)
        if score < 0.5 or match_length < 2:
            continue
        candidates.append((score, match_length, len(normalize_theme_name(name)), code, name))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    score, _, _, code, name = candidates[0]
    return EtfMatch(code=code, name=name, score=score)


def select_market_cap_leaders(
    constituents_df: pd.DataFrame,
    market_df: pd.DataFrame,
    limit: int = 3,
) -> list[StockLeader]:
    """连接板块成分股和全市场快照，按总市值选择龙头。"""
    required_constituent_columns = {"代码", "名称"}
    required_market_columns = {"代码", "总市值"}
    if not required_constituent_columns.issubset(constituents_df.columns):
        return []
    if not required_market_columns.issubset(market_df.columns):
        return []

    constituents = constituents_df[["代码", "名称"]].copy()
    market = market_df[["代码", "总市值"]].copy()
    constituents["代码"] = constituents["代码"].astype(str).str.zfill(6)
    market["代码"] = market["代码"].astype(str).str.zfill(6)
    market["总市值"] = pd.to_numeric(market["总市值"], errors="coerce")

    merged = constituents.merge(market, on="代码", how="inner")
    merged.dropna(subset=["总市值"], inplace=True)
    merged = merged[merged["总市值"] > 0]
    merged.drop_duplicates(subset=["代码"], keep="first", inplace=True)
    merged.sort_values(["总市值", "代码"], ascending=[False, True], inplace=True)

    return [
        StockLeader(code=row.代码, name=str(row.名称), market_cap=float(row.总市值))
        for row in merged.head(limit).itertuples(index=False)
    ]


def format_stock_leader(leader: StockLeader) -> str:
    """按紧凑格式展示股票代码、名称和总市值。"""
    market_cap_yi = leader.market_cap / 100_000_000
    return f"{leader.code} {leader.name}（总市值 {market_cap_yi:.2f} 亿元）"


def _validate_source_frame(
    data_frame: pd.DataFrame,
    label: str,
    required_columns: set[str],
) -> pd.DataFrame:
    missing_columns = required_columns.difference(data_frame.columns)
    if data_frame.empty or missing_columns:
        raise ValueError(f"{label}为空或缺少字段：{sorted(missing_columns)}")
    return data_frame


def _resolve_board_symbol(board_name: str, board_names_df: pd.DataFrame) -> str | None:
    required_columns = {"板块名称", "板块代码"}
    if board_names_df.empty or not required_columns.issubset(board_names_df.columns):
        return None

    target_name = normalize_theme_name(board_name)
    candidates: list[tuple[float, str]] = []
    for row in board_names_df[["板块名称", "板块代码"]].itertuples(index=False, name=None):
        candidate_name, candidate_code = str(row[0]), str(row[1])
        normalized_candidate = normalize_theme_name(candidate_name)
        if normalized_candidate == target_name:
            return candidate_code
        candidates.append((SequenceMatcher(None, target_name, normalized_candidate).ratio(), candidate_code))

    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates or candidates[0][0] < 0.85:
        return None
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return candidates[0][1]


class DataEnricher:
    """按需加载共享行情快照，并为单条命中记录补充关联信息。"""

    def __init__(
        self,
        fetch_etfs: Callable[[], pd.DataFrame] = ak.fund_etf_spot_ths,
        fetch_market: Callable[[], pd.DataFrame] = ak.stock_zh_a_spot_em,
        fetch_industry_names: Callable[[], pd.DataFrame] = ak.stock_board_industry_name_em,
        fetch_concept_names: Callable[[], pd.DataFrame] = ak.stock_board_concept_name_em,
        fetch_industry_constituents: Callable[[str], pd.DataFrame] = ak.stock_board_industry_cons_em,
        fetch_concept_constituents: Callable[[str], pd.DataFrame] = ak.stock_board_concept_cons_em,
        retry_times: int = 3,
        retry_delay: float = 0.8,
    ) -> None:
        self.fetch_etfs = fetch_etfs
        self.fetch_market = fetch_market
        self.fetch_industry_names = fetch_industry_names
        self.fetch_concept_names = fetch_concept_names
        self.fetch_industry_constituents = fetch_industry_constituents
        self.fetch_concept_constituents = fetch_concept_constituents
        self.retry_times = retry_times
        self.retry_delay = retry_delay
        self._etf_df: pd.DataFrame | None = None
        self._market_df: pd.DataFrame | None = None
        self._industry_names_df: pd.DataFrame | None = None
        self._concept_names_df: pd.DataFrame | None = None

    def _load_with_retry(
        self,
        label: str,
        fetcher: Callable[[], pd.DataFrame],
    ) -> pd.DataFrame:
        last_error: Exception | None = None
        for attempt in range(1, self.retry_times + 1):
            try:
                return fetcher()
            except Exception as exc:
                last_error = exc
                logging.warning("获取%s失败，第 %s/%s 次，原因：%s", label, attempt, self.retry_times, exc)
                if attempt < self.retry_times:
                    time.sleep(self.retry_delay * attempt)
        raise RuntimeError(f"获取{label}失败") from last_error

    def _get_etfs(self) -> pd.DataFrame:
        if self._etf_df is None:
            loaded_df = self._load_with_retry("ETF 列表", self.fetch_etfs)
            self._etf_df = _validate_source_frame(
                loaded_df,
                "ETF 列表",
                {"基金代码", "基金名称"},
            )
        return self._etf_df

    def _get_market(self) -> pd.DataFrame:
        if self._market_df is None:
            loaded_df = self._load_with_retry("A 股总市值快照", self.fetch_market)
            self._market_df = _validate_source_frame(
                loaded_df,
                "A 股总市值快照",
                {"代码", "总市值"},
            )
        return self._market_df

    def _get_board_names(self, board_type: str) -> pd.DataFrame:
        if board_type == "行业":
            if self._industry_names_df is None:
                loaded_df = self._load_with_retry("行业板块映射", self.fetch_industry_names)
                self._industry_names_df = _validate_source_frame(
                    loaded_df,
                    "行业板块映射",
                    {"板块名称", "板块代码"},
                )
            return self._industry_names_df
        if self._concept_names_df is None:
            loaded_df = self._load_with_retry("概念板块映射", self.fetch_concept_names)
            self._concept_names_df = _validate_source_frame(
                loaded_df,
                "概念板块映射",
                {"板块名称", "板块代码"},
            )
        return self._concept_names_df

    def _fetch_constituents(self, board_type: str, board_symbol: str) -> pd.DataFrame:
        fetcher = (
            self.fetch_industry_constituents if board_type == "行业" else self.fetch_concept_constituents
        )
        loaded_df = self._load_with_retry("板块成分股", lambda: fetcher(board_symbol))
        return _validate_source_frame(loaded_df, "板块成分股", {"代码", "名称"})

    def enrich_record(self, record: dict[str, object]) -> EnrichmentOutcome:
        enriched_record = dict(record)
        for column in ["关联ETF代码", "关联ETF名称", "市值龙头1", "市值龙头2", "市值龙头3"]:
            enriched_record.setdefault(column, "")
        enriched_record["_stock_leaders"] = []
        warnings: list[str] = []
        board_name = str(record["板块名称"])
        board_type = str(record["板块类型"])

        try:
            etf_match = match_related_etf(board_name, self._get_etfs())
            if etf_match is not None:
                enriched_record["关联ETF代码"] = etf_match.code
                enriched_record["关联ETF名称"] = etf_match.name
        except Exception as exc:
            message = f"【{board_type}-{board_name}】ETF 数据暂缺：{exc}"
            logging.warning(message)
            warnings.append(message)

        try:
            board_symbol = _resolve_board_symbol(board_name, self._get_board_names(board_type))
            if board_symbol is None:
                raise ValueError("未找到可靠的东方财富板块映射")
            constituents_df = self._fetch_constituents(board_type, board_symbol)
            leaders = select_market_cap_leaders(constituents_df, self._get_market())
            enriched_record["_stock_leaders"] = [
                {"code": leader.code, "name": leader.name, "market_cap": leader.market_cap}
                for leader in leaders
            ]
            for index, leader in enumerate(leaders, start=1):
                enriched_record[f"市值龙头{index}"] = format_stock_leader(leader)
        except Exception as exc:
            message = f"【{board_type}-{board_name}】市值龙头数据暂缺：{exc}"
            logging.warning(message)
            warnings.append(message)

        return EnrichmentOutcome(record=enriched_record, warnings=tuple(warnings))
