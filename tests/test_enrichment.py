from __future__ import annotations

import pandas as pd

from board_screening.enrichment import (
    DataEnricher,
    format_stock_leader,
    match_related_etf,
    select_market_cap_leaders,
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


def test_market_cap_leaders_are_joined_and_sorted() -> None:
    constituents_df = pd.DataFrame(
        {
            "代码": ["000001", "000002", "000003", "000004"],
            "名称": ["甲公司", "乙公司", "丙公司", "丁公司"],
        }
    )
    market_df = pd.DataFrame(
        {
            "代码": ["000004", "000002", "000003", "000001"],
            "名称": ["丁公司", "乙公司", "丙公司", "甲公司"],
            "总市值": [200e8, 500e8, None, 300e8],
        }
    )

    leaders = select_market_cap_leaders(constituents_df, market_df)

    assert [leader.code for leader in leaders] == ["000002", "000001", "000004"]
    assert format_stock_leader(leaders[0]) == "000002 乙公司（总市值 500.00 亿元）"


def test_market_cap_leaders_return_available_rows_only() -> None:
    constituents_df = pd.DataFrame({"代码": ["1"], "名称": ["甲公司"]})
    market_df = pd.DataFrame({"代码": ["000001"], "名称": ["甲公司"], "总市值": [100e8]})

    leaders = select_market_cap_leaders(constituents_df, market_df)

    assert len(leaders) == 1
    assert leaders[0].code == "000001"


def test_data_enricher_retries_and_caches_shared_snapshots() -> None:
    attempts = {"etf": 0, "market": 0}

    def fetch_etfs() -> pd.DataFrame:
        attempts["etf"] += 1
        if attempts["etf"] < 2:
            raise ConnectionError("临时失败")
        return pd.DataFrame({"基金代码": ["159994"], "基金名称": ["5GETF"]})

    def fetch_market() -> pd.DataFrame:
        attempts["market"] += 1
        return pd.DataFrame(
            {"代码": ["000001", "000002"], "名称": ["甲", "乙"], "总市值": [100e8, 200e8]}
        )

    board_names = pd.DataFrame({"板块名称": ["5G"], "板块代码": ["BK0001"]})
    constituents = pd.DataFrame({"代码": ["000001", "000002"], "名称": ["甲", "乙"]})
    enricher = DataEnricher(
        fetch_etfs=fetch_etfs,
        fetch_market=fetch_market,
        fetch_industry_names=lambda: board_names,
        fetch_concept_names=lambda: board_names,
        fetch_industry_constituents=lambda _: constituents,
        fetch_concept_constituents=lambda _: constituents,
        retry_delay=0,
    )

    first = enricher.enrich_record({"板块类型": "概念", "板块名称": "5G"})
    second = enricher.enrich_record({"板块类型": "概念", "板块名称": "5G"})

    assert first.record["关联ETF代码"] == "159994"
    assert first.record["市值龙头1"].startswith("000002 乙")
    assert first.record["_stock_leaders"][0] == {
        "code": "000002",
        "name": "乙",
        "market_cap": 200e8,
    }
    assert first.warnings == ()
    assert second.record["关联ETF代码"] == "159994"
    assert attempts == {"etf": 2, "market": 1}


def test_data_enricher_degrades_when_external_sources_fail() -> None:
    def fail() -> pd.DataFrame:
        raise ConnectionError("数据源不可用")

    enricher = DataEnricher(
        fetch_etfs=fail,
        fetch_market=fail,
        fetch_industry_names=fail,
        fetch_concept_names=fail,
        fetch_industry_constituents=lambda _: pd.DataFrame(),
        fetch_concept_constituents=lambda _: pd.DataFrame(),
        retry_times=1,
        retry_delay=0,
    )

    outcome = enricher.enrich_record({"板块类型": "行业", "板块名称": "自动化设备"})

    assert outcome.record["关联ETF代码"] == ""
    assert outcome.record["市值龙头1"] == ""
    assert len(outcome.warnings) == 2


def test_data_enricher_treats_empty_payloads_as_source_warnings() -> None:
    empty = lambda: pd.DataFrame()
    enricher = DataEnricher(
        fetch_etfs=empty,
        fetch_market=empty,
        fetch_industry_names=empty,
        fetch_concept_names=empty,
        fetch_industry_constituents=lambda _: pd.DataFrame(),
        fetch_concept_constituents=lambda _: pd.DataFrame(),
        retry_times=1,
        retry_delay=0,
    )

    outcome = enricher.enrich_record({"板块类型": "概念", "板块名称": "5G"})

    assert len(outcome.warnings) == 2
    assert "ETF 数据暂缺" in outcome.warnings[0]
    assert "市值龙头数据暂缺" in outcome.warnings[1]
