from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import database_connection, database_path, initialize_database  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the reconciliation SQLite schema and optional sample data.")
    parser.add_argument("--seed", action="store_true", help="Insert the exercise's sample transactions if they are not already present.")
    parser.add_argument("--database", help="Database path. Defaults to DATABASE_PATH or data/reconciliation.db.")
    args = parser.parse_args()
    path = database_path(args.database)
    initialize_database(path)
    if args.seed:
        seed_path = ROOT / "sql" / "seed.sql"
        with database_connection(path) as connection:
            connection.executescript(seed_path.read_text(encoding="utf-8"))
    print(f"Database ready: {path}")
    if args.seed:
        print("Sample transactions inserted (existing transaction IDs were left unchanged).")


if __name__ == "__main__":
    main()
