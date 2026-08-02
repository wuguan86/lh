"""Web 服务与持久化配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    session_secret: str
    database_path: Path
    output_file: Path
    divergence_output_file: Path | None = None
    cookie_secure: bool = False
    enable_scheduler: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        password = os.getenv("APP_PASSWORD")
        if not password:
            raise ValueError("必须设置 APP_PASSWORD")
        session_secret = os.getenv("SESSION_SECRET", "")
        if len(session_secret) < 32:
            raise ValueError("SESSION_SECRET 必须至少包含 32 个字符")
        return cls(
            username=os.getenv("APP_USERNAME", "admin"),
            password=password,
            session_secret=session_secret,
            database_path=Path(os.getenv("DATABASE_PATH", "data/screening.db")),
            output_file=Path(os.getenv("OUTPUT_FILE", "data/ths_board_screen_result.csv")),
            divergence_output_file=Path(
                os.getenv(
                    "DIVERGENCE_OUTPUT_FILE",
                    "data/ths_board_macd_divergence_result.csv",
                )
            ),
            cookie_secure=_env_bool("COOKIE_SECURE", False),
            enable_scheduler=_env_bool("ENABLE_SCHEDULER", True),
        )
