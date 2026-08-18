"""筛选任务互斥执行、状态持久化与交易日去重。"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Iterable, Mapping

from board_screening.models import ScreeningOutput
from board_screening.storage import RunRepository
from board_screening.strategies import (
    STRATEGY_EQUAL_DECLINE,
    UNIVERSE_BOARD,
    validate_run_mode,
)


class RunAlreadyActive(RuntimeError):
    """已有筛选任务运行时拒绝重复提交。"""


class RunCoordinator:
    """串行执行筛选任务，并维护完整的运行状态转换。"""

    def __init__(
        self,
        repository: RunRepository,
        screening_callable: Callable[[], ScreeningOutput],
        csv_writer: Callable[[Iterable[dict[str, object]]], None],
        retention_days: int = 90,
        strategy_screeners: Mapping[str, Callable[[], ScreeningOutput]] | None = None,
        strategy_csv_writers: Mapping[
            str, Callable[[Iterable[dict[str, object]]], None]
        ]
        | None = None,
        mode_screeners: Mapping[tuple[str, str], Callable[[], ScreeningOutput]] | None = None,
        mode_csv_writers: Mapping[
            tuple[str, str], Callable[[Iterable[dict[str, object]]], None]
        ]
        | None = None,
    ) -> None:
        self.repository = repository
        self.screening_callable = screening_callable
        self.csv_writer = csv_writer
        self.retention_days = retention_days
        self.mode_screeners = {
            (UNIVERSE_BOARD, strategy): callable_
            for strategy, callable_ in (strategy_screeners or {}).items()
        }
        self.mode_screeners.update(mode_screeners or {})
        self.mode_csv_writers = {
            (UNIVERSE_BOARD, strategy): callable_
            for strategy, callable_ in (strategy_csv_writers or {}).items()
        }
        self.mode_csv_writers.update(mode_csv_writers or {})
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="board-screening")
        self._state_lock = threading.Lock()
        self._idle_condition = threading.Condition(self._state_lock)
        self._is_submitting = False
        self._active_run_id: int | None = None
        self._active_strategy: str | None = None
        self._active_universe: str | None = None
        self._future: Future[None] | None = None
        self._idle_callbacks: list[Callable[[], None]] = []

    @property
    def active_run_id(self) -> int | None:
        with self._state_lock:
            return self._active_run_id

    @property
    def active_strategy(self) -> str | None:
        with self._state_lock:
            return self._active_strategy

    @property
    def active_universe(self) -> str | None:
        with self._state_lock:
            return self._active_universe

    def submit(
        self,
        trigger_type: str,
        strategy: str = STRATEGY_EQUAL_DECLINE,
        universe: str = UNIVERSE_BOARD,
    ) -> int:
        validate_run_mode(strategy, universe)
        with self._idle_condition:
            if self._is_submitting or self._active_run_id is not None:
                raise RunAlreadyActive("已有筛选任务正在执行")
            self._is_submitting = True
        run_id: int | None = None
        try:
            if strategy == STRATEGY_EQUAL_DECLINE and universe == UNIVERSE_BOARD:
                # 默认策略沿用旧调用形式，兼容已有仓储替身和外部调用方。
                run_id = self.repository.create_run(trigger_type)
            else:
                run_id = self.repository.create_run(
                    trigger_type,
                    strategy=strategy,
                    universe=universe,
                )
            with self._idle_condition:
                self._active_run_id = run_id
                self._active_strategy = strategy
                self._active_universe = universe
                self._is_submitting = False
            self._future = self._executor.submit(self._execute, run_id, strategy, universe)
            return run_id
        except Exception as exc:
            if run_id is not None:
                self.repository.finish_run(run_id, "failed", None, 0, 0, str(exc))
            with self._idle_condition:
                self._is_submitting = False
                self._active_run_id = None
                self._active_strategy = None
                self._active_universe = None
                idle_callbacks = list(self._idle_callbacks)
                self._idle_callbacks.clear()
                self._idle_condition.notify_all()
            for callback in idle_callbacks:
                callback()
            raise

    def _execute(self, run_id: int, strategy: str, universe: str) -> None:
        try:
            self.repository.mark_running(run_id)
            screening_callable = self.mode_screeners.get(
                (universe, strategy),
                self.screening_callable,
            )
            output = screening_callable()
            self.repository.save_results(run_id, output.records)
            csv_writer = self.mode_csv_writers.get((universe, strategy), self.csv_writer)
            csv_writer(output.records)
            status = "succeeded_with_warnings" if output.warnings else "succeeded"
            self.repository.finish_run(
                run_id,
                status,
                output.latest_trade_date,
                len(output.records),
                len(output.warnings),
            )
            self.repository.cleanup_old_runs(self.retention_days)
            logging.info("筛选任务 %s 执行完成，命中 %s 个标的。", run_id, len(output.records))
        except Exception as exc:
            logging.exception("筛选任务 %s 失败，原因：%s", run_id, exc)
            self.repository.finish_run(run_id, "failed", None, 0, 0, str(exc))
        finally:
            with self._idle_condition:
                self._active_run_id = None
                self._active_strategy = None
                self._active_universe = None
                idle_callbacks = list(self._idle_callbacks)
                self._idle_callbacks.clear()
            for callback in idle_callbacks:
                try:
                    callback()
                except Exception as exc:
                    logging.warning("空闲后补提任务失败，原因：%s", exc)
            with self._idle_condition:
                self._idle_condition.notify_all()

    def run_after_idle(self, callback: Callable[[], None]) -> None:
        """任务繁忙时登记一次性回调，空闲时立即执行。"""
        with self._idle_condition:
            if self._is_submitting or self._active_run_id is not None:
                if callback not in self._idle_callbacks:
                    self._idle_callbacks.append(callback)
                return
        callback()

    def wait_for_idle(self, timeout: float | None = None) -> None:
        with self._idle_condition:
            is_idle = self._idle_condition.wait_for(
                lambda: not self._is_submitting
                and self._active_run_id is None
                and not self._idle_callbacks,
                timeout=timeout,
            )
        if not is_idle:
            raise TimeoutError("等待筛选任务完成超时")

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)


class ScheduledRunService:
    """根据最新交易日决定是否提交自动筛选任务。"""

    def __init__(
        self,
        repository: RunRepository,
        coordinator: RunCoordinator,
        latest_trade_date_provider: Callable[[], str],
        strategies: tuple[str, ...] = (STRATEGY_EQUAL_DECLINE,),
        modes: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        self.repository = repository
        self.coordinator = coordinator
        self.latest_trade_date_provider = latest_trade_date_provider
        configured_modes = modes or tuple((UNIVERSE_BOARD, strategy) for strategy in strategies)
        for universe, strategy in configured_modes:
            validate_run_mode(strategy, universe)
        self.modes = configured_modes

    def check_and_submit(self) -> int | None:
        try:
            latest_trade_date = self.latest_trade_date_provider()
        except Exception as exc:
            logging.warning("获取最新交易日失败，本次自动检查已跳过，原因：%s", exc)
            return None
        for universe, strategy in self.modes:
            if self.repository.has_successful_trade_date(latest_trade_date, strategy, universe):
                continue
            try:
                run_id = self.coordinator.submit("scheduled", strategy, universe)
                # 当前策略结束后继续检查下一策略，全部完成时该回调会自然退出。
                self.coordinator.run_after_idle(self.check_and_submit)
                return run_id
            except RunAlreadyActive:
                logging.info("已有筛选任务运行，将在任务空闲后补提自动筛选。")
                self.coordinator.run_after_idle(self.check_and_submit)
                return None
        logging.info("交易日 %s 的全部目标策略已有成功结果，本次不重复执行。", latest_trade_date)
        return None
