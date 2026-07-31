from __future__ import annotations

import threading

import pytest

from board_screening.jobs import RunAlreadyActive, RunCoordinator, ScheduledRunService
from board_screening.models import ScreeningOutput
from board_screening.storage import RunRepository


def test_coordinator_persists_success_and_writes_latest_csv(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    written_records: list[dict[str, object]] = []
    output = ScreeningOutput(
        records=(
            {
                "板块类型": "概念",
                "板块名称": "5G",
                "最新交易日": "2026-07-24",
                "当前价格": 95.0,
                "目标位价格": 100.0,
                "目标偏离率": "5.00%",
            },
        ),
        warnings=(),
        latest_trade_date="2026-07-24",
    )
    coordinator = RunCoordinator(repository, lambda: output, written_records.extend)

    run_id = coordinator.submit("manual")
    coordinator.wait_for_idle(timeout=2)

    assert repository.get_run(run_id)["status"] == "succeeded"
    assert repository.get_results(run_id)[0]["板块名称"] == "5G"
    assert written_records[0]["板块名称"] == "5G"
    coordinator.shutdown()


def test_coordinator_marks_warnings_and_failures(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    warning_output = ScreeningOutput(records=(), warnings=("ETF数据暂缺",), latest_trade_date="2026-07-24")
    warning_coordinator = RunCoordinator(repository, lambda: warning_output, lambda _: None)
    warning_run_id = warning_coordinator.submit("manual")
    warning_coordinator.wait_for_idle(timeout=2)

    def fail_screening() -> ScreeningOutput:
        raise RuntimeError("核心行情失败")

    failure_coordinator = RunCoordinator(repository, fail_screening, lambda _: None)
    failure_run_id = failure_coordinator.submit("manual")
    failure_coordinator.wait_for_idle(timeout=2)

    assert repository.get_run(warning_run_id)["status"] == "succeeded_with_warnings"
    failed_run = repository.get_run(failure_run_id)
    assert failed_run["status"] == "failed"
    assert failed_run["error_message"] == "核心行情失败"
    warning_coordinator.shutdown()
    failure_coordinator.shutdown()


def test_coordinator_rejects_overlapping_run(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    release = threading.Event()

    def blocking_screening() -> ScreeningOutput:
        release.wait(timeout=2)
        return ScreeningOutput(records=(), warnings=(), latest_trade_date="2026-07-24")

    coordinator = RunCoordinator(repository, blocking_screening, lambda _: None)
    coordinator.submit("manual")

    with pytest.raises(RunAlreadyActive):
        coordinator.submit("manual")

    release.set()
    coordinator.wait_for_idle(timeout=2)
    coordinator.shutdown()


def test_scheduled_service_skips_processed_trade_date(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    old_run_id = repository.create_run("scheduled")
    repository.finish_run(old_run_id, "succeeded", "2026-07-24", 0, 0)
    coordinator = RunCoordinator(
        repository,
        lambda: ScreeningOutput(records=(), warnings=(), latest_trade_date="2026-07-24"),
        lambda _: None,
    )
    service = ScheduledRunService(repository, coordinator, lambda: "2026-07-24")

    assert service.check_and_submit() is None
    coordinator.shutdown()


def test_manual_run_does_not_suppress_scheduled_close_run(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    manual_run_id = repository.create_run("manual")
    repository.finish_run(manual_run_id, "succeeded", "2026-07-24", 1, 0)
    coordinator = RunCoordinator(
        repository,
        lambda: ScreeningOutput(records=(), warnings=(), latest_trade_date="2026-07-24"),
        lambda _: None,
    )
    service = ScheduledRunService(repository, coordinator, lambda: "2026-07-24")

    scheduled_run_id = service.check_and_submit()
    coordinator.wait_for_idle(timeout=2)

    assert scheduled_run_id is not None
    assert repository.get_run(scheduled_run_id)["trigger_type"] == "scheduled"
    coordinator.shutdown()


def test_scheduled_run_is_submitted_after_active_manual_run_finishes(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    release = threading.Event()
    execution_count = 0

    def screening() -> ScreeningOutput:
        nonlocal execution_count
        execution_count += 1
        if execution_count == 1:
            release.wait(timeout=2)
        return ScreeningOutput(records=(), warnings=(), latest_trade_date="2026-07-24")

    coordinator = RunCoordinator(repository, screening, lambda _: None)
    service = ScheduledRunService(repository, coordinator, lambda: "2026-07-24")
    coordinator.submit("manual")

    assert service.check_and_submit() is None
    release.set()
    coordinator.wait_for_idle(timeout=3)

    assert execution_count == 2
    assert repository.get_current_run()["trigger_type"] == "scheduled"
    coordinator.shutdown()


def test_scheduled_service_logs_calendar_failure(tmp_path, caplog) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    coordinator = RunCoordinator(
        repository,
        lambda: ScreeningOutput(records=(), warnings=(), latest_trade_date="2026-07-24"),
        lambda _: None,
    )
    service = ScheduledRunService(
        repository,
        coordinator,
        lambda: (_ for _ in ()).throw(ConnectionError("日历不可用")),
    )

    assert service.check_and_submit() is None
    assert "获取最新交易日失败" in caplog.text
    coordinator.shutdown()


def test_idle_callback_waits_during_atomic_submission(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    original_create_run = repository.create_run
    create_started = threading.Event()
    release_create = threading.Event()
    callback_called = threading.Event()

    def delayed_create_run(trigger_type: str, started_at=None) -> int:
        create_started.set()
        release_create.wait(timeout=2)
        return original_create_run(trigger_type, started_at)

    repository.create_run = delayed_create_run
    coordinator = RunCoordinator(
        repository,
        lambda: ScreeningOutput(records=(), warnings=(), latest_trade_date="2026-07-24"),
        lambda _: None,
    )
    submit_thread = threading.Thread(target=lambda: coordinator.submit("manual"))
    submit_thread.start()
    assert create_started.wait(timeout=1)

    coordinator.run_after_idle(callback_called.set)
    assert not callback_called.is_set()

    release_create.set()
    submit_thread.join(timeout=2)
    coordinator.wait_for_idle(timeout=2)
    assert callback_called.is_set()
    coordinator.shutdown()
