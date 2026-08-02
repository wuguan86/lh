"""ETF 名称匹配与关联信息补充。"""

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


def _validate_source_frame(
    data_frame: pd.DataFrame,
    label: str,
    required_columns: set[str],
) -> pd.DataFrame:
    missing_columns = required_columns.difference(data_frame.columns)
    if data_frame.empty or missing_columns:
        raise ValueError(f"{label}为空或缺少字段：{sorted(missing_columns)}")
    return data_frame


class DataEnricher:
    """按需加载 ETF 列表，并为单条命中记录补充关联 ETF。"""

    def __init__(
        self,
        fetch_etfs: Callable[[], pd.DataFrame] = ak.fund_etf_spot_ths,
        retry_times: int = 3,
        retry_delay: float = 0.8,
    ) -> None:
        self.fetch_etfs = fetch_etfs
        self.retry_times = retry_times
        self.retry_delay = retry_delay
        self._etf_df: pd.DataFrame | None = None

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

    def enrich_record(self, record: dict[str, object]) -> EnrichmentOutcome:
        enriched_record = dict(record)
        for column in ["关联ETF代码", "关联ETF名称"]:
            enriched_record.setdefault(column, "")
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

        return EnrichmentOutcome(record=enriched_record, warnings=tuple(warnings))
