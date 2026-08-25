from __future__ import annotations

import threading

import pytest

from board_screening.jobs import RunAlreadyActive, RunCoordinator, ScheduledRunService
from board_screening.models import ScreeningOutput
from board_screening.storage import RunRepository
from board_screening.strategies import (
    STRATEGY_EQUAL_DECLINE,
    STRATEGY_MACD_DIVERGENCE,
    SUPPORTED_RUN_MODES,
    UNIVERSE_BOARD,
    UNIVERSE_STOCK,
)


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
                "1:1等距目标价": 100.0,
                "1.272扩展目标价": 86.4,
                "1.618扩展目标价": 69.1,
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

    assert coordinator.active_strategy == STRATEGY_EQUAL_DECLINE
    assert coordinator.active_universe == UNIVERSE_BOARD

    with pytest.raises(RunAlreadyActive):
        coordinator.submit("manual")

    release.set()
    coordinator.wait_for_idle(timeout=2)
    assert coordinator.active_universe is None
    coordinator.shutdown()


def test_coordinator_passes_historical_date_to_selected_board_screener(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    received_dates: list[str] = []

    def historical_screening(target_trade_date: str) -> ScreeningOutput:
        received_dates.append(target_trade_date)
        return ScreeningOutput(records=(), warnings=(), latest_trade_date=target_trade_date)

    coordinator = RunCoordinator(
        repository,
        lambda: ScreeningOutput(records=(), warnings=(), latest_trade_date="2026-08-25"),
        lambda _: None,
        historical_mode_screeners={
            (UNIVERSE_BOARD, STRATEGY_EQUAL_DECLINE): historical_screening,
        },
    )

    run_id = coordinator.submit(
        "manual",
        STRATEGY_EQUAL_DECLINE,
        UNIVERSE_BOARD,
        "2026-07-24",
    )
    coordinator.wait_for_idle(timeout=2)

    assert received_dates == ["2026-07-24"]
    assert repository.get_run(run_id)["latest_trade_date"] == "2026-07-24"
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


def test_scheduled_service_runs_both_strategies_in_sequence(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    executions: list[str] = []

    def output_for(strategy: str):
        def execute() -> ScreeningOutput:
            executions.append(strategy)
            return ScreeningOutput(records=(), warnings=(), latest_trade_date="2026-07-24")

        return execute

    coordinator = RunCoordinator(
        repository,
        output_for("equal_decline"),
        lambda _: None,
        strategy_screeners={
            "equal_decline": output_for("equal_decline"),
            STRATEGY_MACD_DIVERGENCE: output_for(STRATEGY_MACD_DIVERGENCE),
        },
    )
    service = ScheduledRunService(
        repository,
        coordinator,
        lambda: "2026-07-24",
        strategies=("equal_decline", STRATEGY_MACD_DIVERGENCE),
    )

    first_run_id = service.check_and_submit()
    coordinator.wait_for_idle(timeout=3)

    assert first_run_id is not None
    assert executions == ["equal_decline", STRATEGY_MACD_DIVERGENCE]
    assert len(repository.get_runs()) == 2
    coordinator.shutdown()


def test_scheduled_service_runs_all_four_modes_in_sequence(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    executions: list[tuple[str, str]] = []

    def output_for(universe: str, strategy: str):
        def execute() -> ScreeningOutput:
            executions.append((universe, strategy))
            return ScreeningOutput(records=(), warnings=(), latest_trade_date="2026-07-24")

        return execute

    coordinator = RunCoordinator(
        repository,
        output_for(UNIVERSE_BOARD, STRATEGY_EQUAL_DECLINE),
        lambda _: None,
        mode_screeners={mode: output_for(*mode) for mode in SUPPORTED_RUN_MODES},
    )
    service = ScheduledRunService(
        repository,
        coordinator,
        lambda: "2026-07-24",
        modes=SUPPORTED_RUN_MODES,
    )

    service.check_and_submit()
    coordinator.wait_for_idle(timeout=5)

    assert executions == list(SUPPORTED_RUN_MODES)
    assert len(repository.get_runs()) == 4
    assert {run["universe"] for run in repository.get_runs()} == {
        UNIVERSE_BOARD,
        UNIVERSE_STOCK,
    }
    coordinator.shutdown()


def test_scheduled_service_does_not_retry_failed_mode_in_same_cycle(tmp_path) -> None:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    executions: list[tuple[str, str]] = []
    modes = (
        (UNIVERSE_STOCK, STRATEGY_EQUAL_DECLINE),
        (UNIVERSE_STOCK, STRATEGY_MACD_DIVERGENCE),
    )

    def fail_equal_decline() -> ScreeningOutput:
        executions.append(modes[0])
        raise RuntimeError("市值快照失败")

    def succeed_divergence() -> ScreeningOutput:
        executions.append(modes[1])
        return ScreeningOutput(records=(), warnings=(), latest_trade_date="2026-07-24")

    coordinator = RunCoordinator(
        repository,
        fail_equal_decline,
        lambda _: None,
        mode_screeners={
            modes[0]: fail_equal_decline,
            modes[1]: succeed_divergence,
        },
    )
    service = ScheduledRunService(
        repository,
        coordinator,
        lambda: "2026-07-24",
        modes=modes,
    )

    service.check_and_submit()
    coordinator.wait_for_idle(timeout=5)

    assert executions == list(modes)
    assert len(repository.get_runs()) == 2
    assert [run["status"] for run in repository.get_runs()] == ["succeeded", "failed"]
    coordinator.shutdown()
