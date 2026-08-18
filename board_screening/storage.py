"""SQLite 运行记录与筛选结果存储。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from board_screening.core import normalize_target_price_fields
from board_screening.strategies import (
    STRATEGY_EQUAL_DECLINE,
    UNIVERSE_BOARD,
    validate_run_mode,
    validate_strategy,
    validate_universe,
)


SUCCESS_STATUSES = ("succeeded", "succeeded_with_warnings")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_percent(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).rstrip("%")) / 100


class RunRepository:
    """封装筛选任务状态和结果数据访问。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    latest_trade_date TEXT,
                    matched_count INTEGER NOT NULL DEFAULT 0,
                    warning_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    strategy TEXT NOT NULL DEFAULT 'equal_decline',
                    universe TEXT NOT NULL DEFAULT 'board'
                );

                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    target_type TEXT NOT NULL,
                    target_code TEXT NOT NULL DEFAULT '',
                    target_name TEXT NOT NULL,
                    latest_trade_date TEXT NOT NULL,
                    current_price REAL,
                    target_price REAL,
                    target_deviation REAL,
                    max_drawdown REAL,
                    etf_code TEXT,
                    etf_name TEXT,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_runs_trade_date ON runs(latest_trade_date, status);
                CREATE INDEX IF NOT EXISTS idx_results_run_id ON results(run_id);
                """
            )
            run_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "strategy" not in run_columns:
                # 旧数据库中的历史任务均为等距下跌，迁移时保持原有业务语义。
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN strategy TEXT NOT NULL DEFAULT 'equal_decline'"
                )
            if "universe" not in run_columns:
                # 旧任务全部来自板块筛选，迁移时保持原有范围。
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN universe TEXT NOT NULL DEFAULT 'board'"
                )
            result_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(results)").fetchall()
            }
            if "board_type" in result_columns and "target_type" not in result_columns:
                connection.execute("ALTER TABLE results RENAME COLUMN board_type TO target_type")
            if "board_name" in result_columns and "target_name" not in result_columns:
                connection.execute("ALTER TABLE results RENAME COLUMN board_name TO target_name")
            if "target_code" not in result_columns:
                connection.execute(
                    "ALTER TABLE results ADD COLUMN target_code TEXT NOT NULL DEFAULT ''"
                )

    def create_run(
        self,
        trigger_type: str,
        started_at: datetime | None = None,
        strategy: str = STRATEGY_EQUAL_DECLINE,
        universe: str = UNIVERSE_BOARD,
    ) -> int:
        validate_run_mode(strategy, universe)
        started_at = started_at or _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs (trigger_type, status, started_at, strategy, universe)
                VALUES (?, 'queued', ?, ?, ?)
                """,
                (trigger_type, started_at.isoformat(), strategy, universe),
            )
            return int(cursor.lastrowid)

    def mark_running(self, run_id: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE runs SET status = 'running' WHERE id = ?", (run_id,))

    def finish_run(
        self,
        run_id: int,
        status: str,
        latest_trade_date: str | None,
        matched_count: int,
        warning_count: int,
        error_message: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?, latest_trade_date = ?, matched_count = ?,
                    warning_count = ?, error_message = ?
                WHERE id = ?
                """,
                (
                    status,
                    _utc_now().isoformat(),
                    latest_trade_date,
                    matched_count,
                    warning_count,
                    error_message,
                    run_id,
                ),
            )

    def save_results(self, run_id: int, records: Iterable[dict[str, object]]) -> None:
        with self._connect() as connection:
            for record in records:
                public_record = {key: value for key, value in record.items() if not key.startswith("_")}
                if "股票名称" in record:
                    target_type = "个股"
                    target_code = str(record.get("股票代码", ""))
                    target_name = str(record["股票名称"])
                else:
                    target_type = str(record["板块类型"])
                    target_code = ""
                    target_name = str(record["板块名称"])
                connection.execute(
                    """
                    INSERT INTO results (
                        run_id, target_type, target_code, target_name, latest_trade_date, current_price,
                        target_price, target_deviation, max_drawdown, etf_code, etf_name, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        target_type,
                        target_code,
                        target_name,
                        record["最新交易日"],
                        record.get("当前价格"),
                        record.get("1:1等距目标价", record.get("目标位价格")),
                        _parse_percent(record.get("目标偏离率")),
                        _parse_percent(record.get("最大跌幅")),
                        record.get("关联ETF代码", ""),
                        record.get("关联ETF名称", ""),
                        json.dumps(public_record, ensure_ascii=False),
                    ),
                )

    def get_run(self, run_id: int) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_runs(
        self,
        limit: int | None = None,
        retention_days: int = 90,
        strategy: str | None = None,
        universe: str | None = None,
    ) -> list[dict[str, object]]:
        cutoff = (_utc_now() - timedelta(days=retention_days)).isoformat()
        sql = "SELECT * FROM runs WHERE started_at >= ?"
        parameters: tuple[object, ...] = (cutoff,)
        if strategy is not None:
            validate_strategy(strategy)
            sql += " AND strategy = ?"
            parameters += (strategy,)
        if universe is not None:
            validate_universe(universe)
            sql += " AND universe = ?"
            parameters += (universe,)
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            parameters += (limit,)
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]

    def get_current_run(
        self,
        strategy: str | None = None,
        universe: str | None = None,
    ) -> dict[str, object] | None:
        runs = self.get_runs(limit=1, strategy=strategy, universe=universe)
        return runs[0] if runs else None

    def get_latest_successful_run(
        self,
        strategy: str = STRATEGY_EQUAL_DECLINE,
        universe: str = UNIVERSE_BOARD,
    ) -> dict[str, object] | None:
        validate_run_mode(strategy, universe)
        placeholders = ",".join("?" for _ in SUCCESS_STATUSES)
        cutoff = (_utc_now() - timedelta(days=90)).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM runs
                WHERE status IN ({placeholders}) AND started_at >= ?
                    AND strategy = ? AND universe = ?
                ORDER BY id DESC LIMIT 1
                """,
                (*SUCCESS_STATUSES, cutoff, strategy, universe),
            ).fetchone()
        return dict(row) if row else None

    def get_results(self, run_id: int) -> list[dict[str, object]]:
        with self._connect() as connection:
            run_row = connection.execute(
                "SELECT strategy FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            strategy = run_row["strategy"] if run_row else STRATEGY_EQUAL_DECLINE
            result_rows = connection.execute(
                """
                SELECT * FROM results
                WHERE run_id = ?
                ORDER BY target_type, ABS(target_deviation), target_name
                """,
                (run_id,),
            ).fetchall()
            records: list[dict[str, object]] = []
            for row in result_rows:
                record = json.loads(row["payload_json"])
                if strategy == STRATEGY_EQUAL_DECLINE:
                    record = normalize_target_price_fields(record)
                    record["目标偏离率数值"] = row["target_deviation"]
                    record["最大跌幅数值"] = row["max_drawdown"]
                records.append(record)
        return records

    def has_successful_trade_date(
        self,
        trade_date: str,
        strategy: str = STRATEGY_EQUAL_DECLINE,
        universe: str = UNIVERSE_BOARD,
    ) -> bool:
        validate_run_mode(strategy, universe)
        placeholders = ",".join("?" for _ in SUCCESS_STATUSES)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT 1 FROM runs
                WHERE latest_trade_date = ? AND trigger_type = 'scheduled'
                    AND status IN ({placeholders}) AND strategy = ?
                    AND universe = ?
                LIMIT 1
                """,
                (trade_date, *SUCCESS_STATUSES, strategy, universe),
            ).fetchone()
        return row is not None

    def cleanup_old_runs(self, retention_days: int) -> int:
        cutoff = (_utc_now() - timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
            return cursor.rowcount
