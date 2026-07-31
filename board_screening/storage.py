"""SQLite 运行记录与筛选结果存储。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


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
    """封装运行状态、结果和龙头股票明细的数据库访问。"""

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
                    error_message TEXT
                );

                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    board_type TEXT NOT NULL,
                    board_name TEXT NOT NULL,
                    latest_trade_date TEXT NOT NULL,
                    current_price REAL,
                    target_price REAL,
                    target_deviation REAL,
                    max_drawdown REAL,
                    etf_code TEXT,
                    etf_name TEXT,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS stock_leaders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    result_id INTEGER NOT NULL REFERENCES results(id) ON DELETE CASCADE,
                    rank INTEGER NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    market_cap REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_runs_trade_date ON runs(latest_trade_date, status);
                CREATE INDEX IF NOT EXISTS idx_results_run_id ON results(run_id);
                """
            )

    def create_run(self, trigger_type: str, started_at: datetime | None = None) -> int:
        started_at = started_at or _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO runs (trigger_type, status, started_at) VALUES (?, 'queued', ?)",
                (trigger_type, started_at.isoformat()),
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
                cursor = connection.execute(
                    """
                    INSERT INTO results (
                        run_id, board_type, board_name, latest_trade_date, current_price,
                        target_price, target_deviation, max_drawdown, etf_code, etf_name, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        record["板块类型"],
                        record["板块名称"],
                        record["最新交易日"],
                        record.get("当前价格"),
                        record.get("目标位价格"),
                        _parse_percent(record.get("目标偏离率")),
                        _parse_percent(record.get("最大跌幅")),
                        record.get("关联ETF代码", ""),
                        record.get("关联ETF名称", ""),
                        json.dumps(public_record, ensure_ascii=False),
                    ),
                )
                result_id = int(cursor.lastrowid)
                for rank, leader in enumerate(record.get("_stock_leaders", []), start=1):
                    connection.execute(
                        """
                        INSERT INTO stock_leaders
                            (result_id, rank, stock_code, stock_name, market_cap)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (result_id, rank, leader["code"], leader["name"], leader["market_cap"]),
                    )

    def get_run(self, run_id: int) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_runs(self, limit: int | None = None, retention_days: int = 90) -> list[dict[str, object]]:
        cutoff = (_utc_now() - timedelta(days=retention_days)).isoformat()
        sql = "SELECT * FROM runs WHERE started_at >= ? ORDER BY id DESC"
        parameters: tuple[object, ...] = (cutoff,)
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (cutoff, limit)
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]

    def get_current_run(self) -> dict[str, object] | None:
        runs = self.get_runs(limit=1)
        return runs[0] if runs else None

    def get_latest_successful_run(self) -> dict[str, object] | None:
        placeholders = ",".join("?" for _ in SUCCESS_STATUSES)
        cutoff = (_utc_now() - timedelta(days=90)).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM runs
                WHERE status IN ({placeholders}) AND started_at >= ?
                ORDER BY id DESC LIMIT 1
                """,
                (*SUCCESS_STATUSES, cutoff),
            ).fetchone()
        return dict(row) if row else None

    def get_results(self, run_id: int) -> list[dict[str, object]]:
        with self._connect() as connection:
            result_rows = connection.execute(
                """
                SELECT * FROM results
                WHERE run_id = ?
                ORDER BY board_type, ABS(target_deviation), board_name
                """,
                (run_id,),
            ).fetchall()
            records: list[dict[str, object]] = []
            for row in result_rows:
                record = json.loads(row["payload_json"])
                record["目标偏离率数值"] = row["target_deviation"]
                record["最大跌幅数值"] = row["max_drawdown"]
                leader_rows = connection.execute(
                    "SELECT * FROM stock_leaders WHERE result_id = ? ORDER BY rank",
                    (row["id"],),
                ).fetchall()
                record["龙头股票"] = [
                    {
                        "rank": leader["rank"],
                        "code": leader["stock_code"],
                        "name": leader["stock_name"],
                        "market_cap": leader["market_cap"],
                    }
                    for leader in leader_rows
                ]
                records.append(record)
        return records

    def has_successful_trade_date(self, trade_date: str) -> bool:
        placeholders = ",".join("?" for _ in SUCCESS_STATUSES)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT 1 FROM runs
                WHERE latest_trade_date = ? AND trigger_type = 'scheduled'
                    AND status IN ({placeholders})
                LIMIT 1
                """,
                (trade_date, *SUCCESS_STATUSES),
            ).fetchone()
        return row is not None

    def cleanup_old_runs(self, retention_days: int) -> int:
        cutoff = (_utc_now() - timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
            return cursor.rowcount
