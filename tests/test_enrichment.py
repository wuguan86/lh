from __future__ import annotations

import pandas as pd

from board_screening.enrichment import (
    DataEnricher,
    match_related_etf,
)


def test_etf_match_prefers_shorter_direct_theme_name() -> None:
    etf_df = pd.DataFrame(
        {
            "基金代码": ["159811", "159994", "512480"],
            "基金名称": ["博时5G50ETF", "5GETF", "半导体ETF"],
        }
    )

    match = match_related_etf("5G概念", etf_df)

    assert match is not None
    assert match.code == "159994"
    assert match.name == "5GETF"


def test_etf_match_uses_parenthetical_board_theme() -> None:
    etf_df = pd.DataFrame(
        {
            "基金代码": ["516000", "516001"],
            "基金名称": ["电子烟ETF", "新能源ETF"],
        }
    )

    match = match_related_etf("新型烟草(电子烟)", etf_df)

    assert match is not None
    assert match.code == "516000"


def test_etf_match_rejects_weak_name_overlap() -> None:
    etf_df = pd.DataFrame(
        {
            "基金代码": ["512480"],
            "基金名称": ["半导体ETF"],
        }
    )

    assert match_related_etf("自动化设备", etf_df) is None


def test_data_enricher_retries_and_caches_etf_list() -> None:
    attempts = {"etf": 0}

    def fetch_etfs() -> pd.DataFrame:
        attempts["etf"] += 1
        if attempts["etf"] < 2:
            raise ConnectionError("临时失败")
        return pd.DataFrame({"基金代码": ["159994"], "基金名称": ["5GETF"]})

    enricher = DataEnricher(
        fetch_etfs=fetch_etfs,
        retry_delay=0,
    )

    first = enricher.enrich_record({"板块类型": "概念", "板块名称": "5G"})
    second = enricher.enrich_record({"板块类型": "概念", "板块名称": "5G"})

    assert first.record["关联ETF代码"] == "159994"
    assert first.warnings == ()
    assert second.record["关联ETF代码"] == "159994"
    assert attempts == {"etf": 2}


def test_data_enricher_degrades_when_external_sources_fail() -> None:
    def fail() -> pd.DataFrame:
        raise ConnectionError("数据源不可用")

    enricher = DataEnricher(
        fetch_etfs=fail,
        retry_times=1,
        retry_delay=0,
    )

    outcome = enricher.enrich_record({"板块类型": "行业", "板块名称": "自动化设备"})

    assert outcome.record["关联ETF代码"] == ""
    assert len(outcome.warnings) == 1


def test_data_enricher_treats_empty_payloads_as_source_warnings() -> None:
    empty = lambda: pd.DataFrame()
    enricher = DataEnricher(
        fetch_etfs=empty,
        retry_times=1,
        retry_delay=0,
    )

    outcome = enricher.enrich_record({"板块类型": "概念", "板块名称": "5G"})

    assert len(outcome.warnings) == 1
    assert "ETF 数据暂缺" in outcome.warnings[0]
