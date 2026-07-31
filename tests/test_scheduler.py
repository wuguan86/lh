from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from board_screening.scheduler import (
    build_scheduler,
    fetch_latest_trade_date,
    latest_trade_date_from_calendar,
    submit_startup_catchup,
)


class FakeScheduledService:
    def __init__(self) -> None:
        self.calls = 0

    def check_and_submit(self) -> int:
        self.calls += 1
        return 7


def test_latest_trade_date_ignores_future_calendar_rows() -> None:
    calendar_df = pd.DataFrame(
        {"trade_date": pd.to_datetime(["2026-07-24", "2026-07-27", "2026-07-28"])}
    )

    assert latest_trade_date_from_calendar(calendar_df, "2026-07-27") == "2026-07-27"


def test_latest_trade_date_uses_previous_closed_day_before_1800() -> None:
    calendar_df = pd.DataFrame(
        {"trade_date": pd.to_datetime(["2026-07-24", "2026-07-27"])}
    )
    morning = datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    evening = datetime(2026, 7, 27, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert fetch_latest_trade_date(lambda: calendar_df, morning) == "2026-07-24"
    assert fetch_latest_trade_date(lambda: calendar_df, evening) == "2026-07-27"


def test_scheduler_registers_daily_job_at_shanghai_1800() -> None:
    service = FakeScheduledService()

    scheduler = build_scheduler(service)
    job = scheduler.get_job("daily-board-screening")

    assert job is not None
    assert str(job.trigger.fields[5]) == "18"
    assert str(job.trigger.fields[6]) == "0"
    assert str(job.trigger.timezone) == "Asia/Shanghai"
    recovery_job = scheduler.get_job("board-screening-recovery-check")
    assert recovery_job is not None
    assert str(recovery_job.trigger.fields[5]) == "18-23"
    assert str(recovery_job.trigger.fields[6]) == "15,30,45"


def test_startup_catchup_waits_until_close_for_current_trade_day() -> None:
    service = FakeScheduledService()
    morning = datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    result = submit_startup_catchup(service, "2026-07-27", morning)

    assert result is None
    assert service.calls == 0


def test_startup_catchup_runs_for_missed_previous_trade_day() -> None:
    service = FakeScheduledService()
    morning = datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    result = submit_startup_catchup(service, "2026-07-24", morning)

    assert result == 7
    assert service.calls == 1
