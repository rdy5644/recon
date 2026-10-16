from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "reconciliation.db"

SCHEMA = (PROJECT_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")


def database_path(configured: str | Path | None = None) -> Path:
    value = configured or os.environ.get("DATABASE_PATH") or DEFAULT_DATABASE_PATH
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


@contextmanager
def database_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    connection = connect(path)
    try:
        yield connection
    finally:
        connection.close()


def initialize_database(path: str | Path) -> None:
    target = database_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with database_connection(target) as connection:
        connection.executescript(SCHEMA)

