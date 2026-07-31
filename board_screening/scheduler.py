"""北京时间交易日判断与每日自动调度。"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from board_screening.jobs import ScheduledRunService


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def latest_trade_date_from_calendar(calendar_df: pd.DataFrame, today: str) -> str:
    """从交易日历中选择不晚于指定日期的最近交易日。"""
    if calendar_df.empty or "trade_date" not in calendar_df.columns:
        raise ValueError("交易日历为空或缺少 trade_date 字段")
    trade_dates = pd.to_datetime(calendar_df["trade_date"], errors="coerce").dropna()
    eligible_dates = trade_dates[trade_dates <= pd.Timestamp(today)]
    if eligible_dates.empty:
        raise ValueError("交易日历中没有可用日期")
    return eligible_dates.max().strftime("%Y-%m-%d")


def fetch_latest_trade_date(
    calendar_fetcher: Callable[[], pd.DataFrame] = ak.tool_trade_date_hist_sina,
    now: datetime | None = None,
) -> str:
    current_time = now or datetime.now(SHANGHAI_TIMEZONE)
    cutoff_date = current_time.date()
    if current_time.timetz().replace(tzinfo=None) < time(18, 0):
        cutoff_date -= timedelta(days=1)
    return latest_trade_date_from_calendar(calendar_fetcher(), cutoff_date.strftime("%Y-%m-%d"))


def build_scheduler(service: ScheduledRunService) -> BackgroundScheduler:
    """注册每天 18:00 的唯一筛选任务，防止误创建重复作业。"""
    scheduler = BackgroundScheduler(timezone=SHANGHAI_TIMEZONE)
    scheduler.add_job(
        service.check_and_submit,
        trigger=CronTrigger(hour=18, minute=0, timezone=SHANGHAI_TIMEZONE),
        id="daily-board-screening",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=6 * 60 * 60,
    )
    scheduler.add_job(
        service.check_and_submit,
        trigger=CronTrigger(hour="18-23", minute="15,30,45", timezone=SHANGHAI_TIMEZONE),
        id="board-screening-recovery-check",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=15 * 60,
    )
    return scheduler


def submit_startup_catchup(
    service: ScheduledRunService,
    latest_trade_date: str,
    now: datetime | None = None,
) -> int | None:
    """容器启动时补跑已收盘的最新交易日，避免盘中提前执行。"""
    current_time = now or datetime.now(SHANGHAI_TIMEZONE)
    trade_date = datetime.strptime(latest_trade_date, "%Y-%m-%d").date()
    if trade_date == current_time.date() and current_time.timetz().replace(tzinfo=None) < time(18, 0):
        return None
    return service.check_and_submit()
