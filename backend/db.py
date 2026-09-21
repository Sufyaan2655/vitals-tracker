"""SQLite persistence: user accounts + per-user session history + the
background job queue that video processing runs through.

Sessions are scoped by `user_id` (a real account, see auth.py), not a typed
username string - that changed once real sign-in replaced the trust-any-
name model. Kept to raw sqlite3, no ORM, at this scale.
"""

import json
import sqlite3
import time
from pathlib import Path

from vitals import (
    BREATHING_RATE_PLAUSIBLE_BPM,
    HEART_RATE_PLAUSIBLE_BPM,
    is_plausible_bpm,
)

DB_PATH = Path(__file__).parent / "vitals.db"


class UsernameTakenError(Exception):
    """Raised by create_user() when the username is already registered."""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            user_id INTEGER,
            created_at REAL NOT NULL,
            heart_rate_bpm REAL NOT NULL,
            heart_rate_confidence REAL NOT NULL,
            breathing_rate_bpm REAL NOT NULL,
            breathing_rate_confidence REAL NOT NULL,
            fps REAL,
            num_frames INTEGER,
            face_detection_rate REAL
        )
        """
    )
    # Migration for a pre-accounts database: the old `sessions` table has no
    # `user_id` column. Add it without touching existing rows - those were
    # written under typed, unauthenticated names before real accounts
    # existed, so there is no account to migrate them to; they simply stop
    # being reachable through the app rather than being deleted.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "user_id" not in existing_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            result_json TEXT,
            error TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def create_user(username: str, password_hash: str) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, time.time()),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise UsernameTakenError(username) from None
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user(user_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def insert_session(user_id: int, result: dict) -> int:
    conn = get_connection()
    cur = conn.execute(
        """
        INSERT INTO sessions (
            user_id, created_at, heart_rate_bpm, heart_rate_confidence,
            breathing_rate_bpm, breathing_rate_confidence, fps, num_frames,
            face_detection_rate
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            time.time(),
            result["heart_rate_bpm"],
            result["heart_rate_confidence"],
            result["breathing_rate_bpm"],
            result["breathing_rate_confidence"],
            result.get("fps"),
            result.get("num_frames"),
            result.get("face_detection_rate"),
        ),
    )
    conn.commit()
    session_id = cur.lastrowid
    conn.close()
    return session_id


def delete_session(session_id: int, user_id: int) -> bool:
    conn = get_connection()
    cur = conn.execute(
        "DELETE FROM sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    )
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def get_sessions(user_id: int) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM sessions WHERE user_id = ? ORDER BY created_at ASC",
        (user_id,),
    ).fetchall()
    conn.close()
    sessions = [dict(row) for row in rows]
    # Plausibility isn't a stored column - it's derived from bpm at read time
    # so historical sessions (recorded before this check existed, or scored
    # against a threshold that later changes) always reflect the current
    # definition instead of a stale flag frozen at insert time.
    for session in sessions:
        session["heart_rate_plausible"] = is_plausible_bpm(session["heart_rate_bpm"], HEART_RATE_PLAUSIBLE_BPM)
        session["breathing_rate_plausible"] = is_plausible_bpm(session["breathing_rate_bpm"], BREATHING_RATE_PLAUSIBLE_BPM)
    return sessions


# ---------------------------------------------------------------------------
# Background job queue: video processing is the compute-heavy part of this
# app, so the upload endpoint creates a row here and returns immediately
# rather than blocking the HTTP request for the duration of processing. A
# FastAPI BackgroundTask (see main.py) does the actual work and updates the
# row through the same three states every job passes through once: pending
# -> processing -> done (with a result) or failed (with an error message).
# ---------------------------------------------------------------------------

def create_job(user_id: int | None) -> int:
    conn = get_connection()
    now = time.time()
    cur = conn.execute(
        "INSERT INTO jobs (user_id, status, created_at, updated_at) VALUES (?, 'pending', ?, ?)",
        (user_id, now, now),
    )
    conn.commit()
    job_id = cur.lastrowid
    conn.close()
    return job_id


def get_job(job_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    job = dict(row)
    result_json = job.pop("result_json")
    job["result"] = json.loads(result_json) if result_json else None
    return job


def update_job(
    job_id: int,
    status: str,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET status = ?, result_json = ?, error = ?, updated_at = ? WHERE id = ?",
        (status, json.dumps(result) if result is not None else None, error, time.time(), job_id),
    )
    conn.commit()
    conn.close()
