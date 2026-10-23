"""CourseVault deployment health check (SQLite edition).

Run with: python healthcheck.py
Exit code 0 means all checks passed; non-zero means a check failed.
Uses only the standard library.
"""

import os
import re
import sqlite3
import sys
from pathlib import Path

REQUIRED_TABLES = {"users", "courses", "wishlist", "audit_logs"}


def load_env_file(path: str = ".env") -> None:
    """Load KEY=VALUE lines from .env without overriding real env vars."""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        os.environ.setdefault(key.strip(), value)


def check_environment() -> bool:
    """Validate required environment variables without printing secrets."""
    required = ("BOT_TOKEN", "OWNER_TELEGRAM_ID")
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        print(f"FAIL: Missing environment variables: {', '.join(missing)}")
        return False

    owner = os.environ["OWNER_TELEGRAM_ID"].strip()
    if not owner.isdigit() or int(owner) <= 0:
        print("FAIL: OWNER_TELEGRAM_ID must be a positive integer.")
        return False

    token = os.environ["BOT_TOKEN"].strip()
    if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{20,}", token):
        print("FAIL: BOT_TOKEN format looks invalid (expected 123456:ABC...).")
        return False
    if len(token) < 20:
        print("FAIL: BOT_TOKEN is too short.")
        return False

    print("PASS: Required environment variables are present.")
    return True


def check_database() -> bool:
    """Check the SQLite file is reachable and, if it exists, healthy."""
    db_path = Path(os.getenv("SQLITE_DB_PATH", "coursevault.db").strip()).expanduser()
    parent = db_path.parent if str(db_path.parent) else Path(".")

    if not db_path.exists():
        if not parent.exists() or not os.access(parent, os.W_OK):
            print(f"FAIL: Cannot create the database in '{parent}' (missing or not writable).")
            return False
        print(f"PASS: Database file not created yet; it will be created at {db_path}.")
        return True

    if not os.access(db_path, os.R_OK | os.W_OK):
        print(f"FAIL: Database file {db_path} is not readable/writable.")
        return False

    try:
        connection = sqlite3.connect(db_path, timeout=10)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                print("FAIL: SQLite integrity check failed.")
                return False
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
    except sqlite3.Error as exc:
        print(f"FAIL: Could not open the SQLite database ({type(exc).__name__}).")
        return False

    missing = REQUIRED_TABLES - tables
    if missing:
        print(f"FAIL: Database is missing tables: {', '.join(sorted(missing))}.")
        return False

    print("PASS: SQLite database opens, passes integrity check, schema present.")
    return True


def main() -> int:
    print("CourseVault deployment health check")
    print("-----------------------------------")
    load_env_file()

    if not check_environment():
        return 1
    if not check_database():
        return 1

    print("-----------------------------------")
    print("PASS: All health checks passed.")
    print("Note: This does not verify Telegram authentication or payment processing.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nHealth check interrupted.")
        sys.exit(130)
