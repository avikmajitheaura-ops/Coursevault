"""CourseVault SQLite data layer.

Uses only the standard library (sqlite3). Every call opens a short-lived
connection inside a worker thread, so the asyncio event loop is never
blocked. Rows are returned as plain dicts.

Public API (used by main.py):
    create_pool, close_pool, upsert_user,
    list_courses, list_categories, get_course,
    list_wishlist, add_to_wishlist,
    create_course_draft, list_course_drafts, get_draft,
    publish_course, write_audit_log
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

logger = logging.getLogger("coursevault.database")

LIST_LIMIT = 30
DRAFT_LIMIT = 50

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    telegram_id  INTEGER PRIMARY KEY,
    username     TEXT,
    first_name   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT ({_NOW}),
    last_seen_at TEXT NOT NULL DEFAULT ({_NOW})
);

CREATE TABLE IF NOT EXISTS courses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    category      TEXT NOT NULL,
    price_paise   INTEGER NOT NULL CHECK (price_paise >= 0),
    description   TEXT NOT NULL DEFAULT '',
    impact        TEXT NOT NULL DEFAULT '',
    prerequisites TEXT NOT NULL DEFAULT '',
    zip_file_id   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'published', 'archived')),
    created_by    INTEGER,
    created_at    TEXT NOT NULL DEFAULT ({_NOW}),
    published_by  INTEGER,
    published_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_courses_status_category
    ON courses (status, category);

CREATE TABLE IF NOT EXISTS wishlist (
    user_id    INTEGER NOT NULL
               REFERENCES users (telegram_id) ON DELETE CASCADE,
    course_id  INTEGER NOT NULL
               REFERENCES courses (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT ({_NOW}),
    PRIMARY KEY (user_id, course_id)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_telegram_id INTEGER,
    action            TEXT NOT NULL,
    target_type       TEXT NOT NULL,
    target_id         TEXT NOT NULL,
    details           TEXT NOT NULL DEFAULT '{{}}',
    created_at        TEXT NOT NULL DEFAULT ({_NOW})
);
"""


class SQLiteDatabase:
    """Handle to a SQLite database file."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection


# --------------------------------------------------
# Internal helpers (blocking; always run via asyncio.to_thread)
# --------------------------------------------------

def _fetchall(
    db: SQLiteDatabase, sql: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    with closing(db.connect()) as connection:
        return [dict(row) for row in connection.execute(sql, params)]


def _fetchone(
    db: SQLiteDatabase, sql: str, params: tuple[Any, ...] = ()
) -> dict[str, Any] | None:
    with closing(db.connect()) as connection:
        row = connection.execute(sql, params).fetchone()
        return dict(row) if row else None


def _insert_audit(
    connection: sqlite3.Connection,
    actor_telegram_id: int | None,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any] | None,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_logs (
            actor_telegram_id, action, target_type, target_id, details
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            actor_telegram_id,
            action,
            target_type,
            target_id,
            json.dumps(details or {}, ensure_ascii=False),
        ),
    )


def _init_schema(db: SQLiteDatabase) -> None:
    with closing(db.connect()) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
        connection.commit()


# --------------------------------------------------
# Lifecycle
# --------------------------------------------------

async def create_pool(path: str | Path) -> SQLiteDatabase:
    """Create the database file (and parent folder) and ensure the schema."""
    db_path = Path(path).expanduser()
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)

    db = SQLiteDatabase(db_path)
    await asyncio.to_thread(_init_schema, db)
    logger.info("SQLite database ready at %s", db.path)
    return db


async def close_pool(db: SQLiteDatabase) -> None:
    """Connections are short-lived, so there is nothing to close."""
    logger.info("SQLite database released (%s)", db.path)


# --------------------------------------------------
# Users
# --------------------------------------------------

def _upsert_user(
    db: SQLiteDatabase, telegram_id: int, username: str | None, first_name: str
) -> None:
    with closing(db.connect()) as connection, connection:
        connection.execute(
            f"""
            INSERT INTO users (telegram_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT (telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_seen_at = {_NOW}
            """,
            (telegram_id, username, first_name),
        )


async def upsert_user(
    db: SQLiteDatabase,
    telegram_id: int,
    username: str | None,
    first_name: str,
) -> None:
    await asyncio.to_thread(_upsert_user, db, telegram_id, username, first_name)


# --------------------------------------------------
# Courses (public)
# --------------------------------------------------

async def list_courses(
    db: SQLiteDatabase, category: str | None = None
) -> list[dict[str, Any]]:
    """Published courses, newest first (capped to avoid flooding the chat)."""
    if category is None:
        return await asyncio.to_thread(
            _fetchall,
            db,
            "SELECT id, title, category, price_paise FROM courses "
            "WHERE status = 'published' ORDER BY id DESC LIMIT ?",
            (LIST_LIMIT,),
        )
    return await asyncio.to_thread(
        _fetchall,
        db,
        "SELECT id, title, category, price_paise FROM courses "
        "WHERE status = 'published' AND category = ? "
        "ORDER BY id DESC LIMIT ?",
        (category, LIST_LIMIT),
    )


async def list_categories(db: SQLiteDatabase) -> list[str]:
    """Distinct categories of published courses, in a stable order."""
    rows = await asyncio.to_thread(
        _fetchall,
        db,
        "SELECT DISTINCT category FROM courses "
        "WHERE status = 'published' ORDER BY category COLLATE NOCASE, category "
        "LIMIT 50",
    )
    return [row["category"] for row in rows]


async def get_course(
    db: SQLiteDatabase, course_id: int
) -> dict[str, Any] | None:
    """A single *published* course. Drafts are never exposed to buyers."""
    return await asyncio.to_thread(
        _fetchone,
        db,
        "SELECT id, title, category, price_paise, description, impact, "
        "prerequisites FROM courses WHERE id = ? AND status = 'published'",
        (course_id,),
    )


# --------------------------------------------------
# Wishlist
# --------------------------------------------------

def _add_to_wishlist(db: SQLiteDatabase, user_id: int, course_id: int) -> bool:
    with closing(db.connect()) as connection, connection:
        # A user can tap "save" without ever sending /start.
        connection.execute(
            "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (user_id,)
        )
        exists = connection.execute(
            "SELECT 1 FROM courses WHERE id = ? AND status = 'published'",
            (course_id,),
        ).fetchone()
        if exists is None:
            return False
        cursor = connection.execute(
            "INSERT OR IGNORE INTO wishlist (user_id, course_id) VALUES (?, ?)",
            (user_id, course_id),
        )
        return cursor.rowcount == 1


async def add_to_wishlist(
    db: SQLiteDatabase, user_id: int, course_id: int
) -> bool:
    """True if newly saved; False if it was already on the wishlist."""
    return await asyncio.to_thread(_add_to_wishlist, db, user_id, course_id)


async def list_wishlist(
    db: SQLiteDatabase, user_id: int
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        _fetchall,
        db,
        """
        SELECT c.id, c.title, c.category, c.price_paise
        FROM wishlist w
        JOIN courses c ON c.id = w.course_id
        WHERE w.user_id = ? AND c.status = 'published'
        ORDER BY w.created_at DESC, c.id DESC
        LIMIT ?
        """,
        (user_id, LIST_LIMIT),
    )


# --------------------------------------------------
# Owner workflow: drafts and publishing
# --------------------------------------------------

def _create_course_draft(
    db: SQLiteDatabase,
    created_by: int,
    title: str,
    category: str,
    price_paise: int,
    description: str,
    impact: str,
    prerequisites: str,
    zip_file_id: str,
    details: dict[str, Any] | None,
) -> int:
    with closing(db.connect()) as connection, connection:
        cursor = connection.execute(
            """
            INSERT INTO courses (
                title, category, price_paise, description, impact,
                prerequisites, zip_file_id, status, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?)
            """,
            (
                title,
                category,
                price_paise,
                description,
                impact,
                prerequisites,
                zip_file_id,
                created_by,
            ),
        )
        course_id = int(cursor.lastrowid)
        # Same transaction: a draft never exists without its audit entry.
        _insert_audit(
            connection,
            created_by,
            "course_draft_created",
            "course",
            str(course_id),
            details,
        )
        return course_id


async def create_course_draft(
    db: SQLiteDatabase,
    *,
    created_by: int,
    title: str,
    category: str,
    price_paise: int,
    description: str,
    impact: str,
    prerequisites: str,
    zip_file_id: str,
    details: dict[str, Any] | None = None,
) -> int:
    return await asyncio.to_thread(
        _create_course_draft,
        db,
        created_by,
        title,
        category,
        price_paise,
        description,
        impact,
        prerequisites,
        zip_file_id,
        details,
    )


async def list_course_drafts(db: SQLiteDatabase) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        _fetchall,
        db,
        "SELECT id, title, category, price_paise FROM courses "
        "WHERE status = 'draft' ORDER BY id DESC LIMIT ?",
        (DRAFT_LIMIT,),
    )


async def get_draft(
    db: SQLiteDatabase, course_id: int
) -> dict[str, Any] | None:
    return await asyncio.to_thread(
        _fetchone,
        db,
        "SELECT id, title, status FROM courses "
        "WHERE id = ? AND status = 'draft'",
        (course_id,),
    )


def _publish_course(db: SQLiteDatabase, user_id: int, course_id: int) -> str:
    with closing(db.connect()) as connection, connection:
        cursor = connection.execute(
            f"""
            UPDATE courses
            SET status = 'published', published_by = ?, published_at = {_NOW}
            WHERE id = ? AND status = 'draft'
            """,
            (user_id, course_id),
        )
        if cursor.rowcount != 1:
            return "not_found"
        _insert_audit(
            connection, user_id, "course_published", "course", str(course_id), None
        )
        return "published"


async def publish_course(
    db: SQLiteDatabase, user_id: int, course_id: int
) -> str:
    """Returns "published" or "not_found" (missing / already published)."""
    return await asyncio.to_thread(_publish_course, db, user_id, course_id)


# --------------------------------------------------
# Audit log
# --------------------------------------------------

def _write_audit_log(
    db: SQLiteDatabase,
    actor_telegram_id: int | None,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any] | None,
) -> None:
    with closing(db.connect()) as connection, connection:
        _insert_audit(
            connection, actor_telegram_id, action, target_type, target_id, details
        )


async def write_audit_log(
    db: SQLiteDatabase,
    actor_telegram_id: int | None,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any] | None = None,
) -> None:
    await asyncio.to_thread(
        _write_audit_log,
        db,
        actor_telegram_id,
        action,
        target_type,
        target_id,
        details,
    )
