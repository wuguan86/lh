from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from board_screening.storage import RunRepository
from board_screening.strategies import STRATEGY_MACD_DIVERGENCE


def _sample_record() -> dict[str, object]:
    return {
        "板块类型": "概念",
        "板块名称": "5G",
        "最新交易日": "2026-07-24",
        "当前价格": 95.0,
        "1:1等距目标价": 100.0,
        "1.272扩展目标价": 86.4,
        "1.618扩展目标价": 69.1,
        "目标偏离率": "5.00%",
        "最大跌幅": "9.00%",
        "关联ETF代码": "159994",
        "关联ETF名称": "5GETF",
    }


def test_repository_saves_run_results(tmp_path) -> None:
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
    assert results[0]["1.618扩展目标价"] == 69.1


def test_repository_adds_extension_targets_to_legacy_results(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    run_id = repository.create_run("manual")
    legacy_record = _sample_record()
    legacy_record["目标位价格"] = legacy_record.pop("1:1等距目标价")
    legacy_record.pop("1.272扩展目标价")
    legacy_record.pop("1.618扩展目标价")
    legacy_record["支撑位"] = 150.0
    legacy_record["最高点价格"] = 200.0

    repository.save_results(run_id, [legacy_record])
    result = repository.get_results(run_id)[0]

    assert result["1:1等距目标价"] == 100.0
    assert result["1.272扩展目标价"] == 86.4
    assert result["1.618扩展目标价"] == 69.1
    assert result["上涨幅度"] == "33.33%"


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


def test_repository_migrates_legacy_runs_to_equal_decline(tmp_path) -> None:
    database_path = tmp_path / "screening.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_type TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                latest_trade_date TEXT,
                matched_count INTEGER NOT NULL DEFAULT 0,
                warning_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO runs (trigger_type, status, started_at) VALUES ('manual', 'queued', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )

    repository = RunRepository(database_path)
    repository.initialize()

    assert repository.get_run(1)["strategy"] == "equal_decline"


def test_repository_isolates_success_and_results_by_strategy(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    run_id = repository.create_run("scheduled", strategy=STRATEGY_MACD_DIVERGENCE)
    repository.save_results(
        run_id,
        [
            {
                "板块类型": "概念",
                "板块名称": "5G",
                "最新交易日": "2026-07-24",
                "当前价格": 95.0,
                "周期": "日线",
                "背离分类": "单纯底背离",
            }
        ],
    )
    repository.finish_run(run_id, "succeeded", "2026-07-24", 1, 0)

    result = repository.get_results(run_id)[0]
    assert result["周期"] == "日线"
    assert "1:1等距目标价" not in result
    assert repository.has_successful_trade_date("2026-07-24", STRATEGY_MACD_DIVERGENCE)
    assert not repository.has_successful_trade_date("2026-07-24")
    assert repository.get_runs(strategy=STRATEGY_MACD_DIVERGENCE)[0]["id"] == run_id
