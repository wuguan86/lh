from __future__ import annotations

from datetime import datetime, timedelta, timezone

from board_screening.storage import RunRepository


def _sample_record() -> dict[str, object]:
    return {
        "板块类型": "概念",
        "板块名称": "5G",
        "最新交易日": "2026-07-24",
        "当前价格": 95.0,
        "目标位价格": 100.0,
        "目标偏离率": "5.00%",
        "最大跌幅": "9.00%",
        "关联ETF代码": "159994",
        "关联ETF名称": "5GETF",
        "市值龙头1": "000001 甲公司（总市值 500.00 亿元）",
        "市值龙头2": "",
        "市值龙头3": "",
        "_stock_leaders": [
            {"code": "000001", "name": "甲公司", "market_cap": 500e8},
        ],
    }


def test_repository_saves_run_results_and_leaders(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    run_id = repository.create_run("manual")

    repository.save_results(run_id, [_sample_record()])
    repository.finish_run(
        run_id,
        status="succeeded",
        latest_trade_date="2026-07-24",
        matched_count=1,
        warning_count=0,
    )

    run = repository.get_run(run_id)
    results = repository.get_results(run_id)

    assert run is not None
    assert run["status"] == "succeeded"
    assert run["matched_count"] == 1
    assert results[0]["板块名称"] == "5G"
    assert results[0]["目标偏离率数值"] == 0.05
    assert results[0]["龙头股票"][0]["code"] == "000001"


def test_repository_detects_successful_trade_date(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    run_id = repository.create_run("scheduled")
    repository.finish_run(run_id, "succeeded_with_warnings", "2026-07-24", 2, 1)

    assert repository.has_successful_trade_date("2026-07-24")
    assert not repository.has_successful_trade_date("2026-07-25")


def test_repository_deletes_runs_older_than_retention(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    old_started_at = datetime.now(timezone.utc) - timedelta(days=91)
    old_run_id = repository.create_run("scheduled", started_at=old_started_at)
    repository.save_results(old_run_id, [_sample_record()])
    recent_run_id = repository.create_run("manual")

    deleted_count = repository.cleanup_old_runs(retention_days=90)

    assert deleted_count == 1
    assert repository.get_run(old_run_id) is None
    assert repository.get_run(recent_run_id) is not None


def test_repository_lists_all_runs_within_ninety_days(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    for _ in range(95):
        repository.create_run("manual")

    assert len(repository.get_runs()) == 95


def test_latest_successful_run_respects_ninety_day_retention(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    old_started_at = datetime.now(timezone.utc) - timedelta(days=91)
    run_id = repository.create_run("scheduled", started_at=old_started_at)
    repository.finish_run(run_id, "succeeded", "2026-04-01", 1, 0)

    assert repository.get_latest_successful_run() is None
