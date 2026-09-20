"""Minimal SQLite persistence layer - one table, no ORM needed at this scale.

Kept deliberately simple for the MVP (no password auth yet - see README for the
"add real accounts" step in the extension roadmap). Sessions are namespaced by
a `username` string so the app already supports multiple people's history
without cross-contaminating trend lines.
"""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "vitals.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
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
    conn.commit()
    conn.close()


def insert_session(username: str, result: dict) -> int:
    conn = get_connection()
    cur = conn.execute(
        """
        INSERT INTO sessions (
            username, created_at, heart_rate_bpm, heart_rate_confidence,
            breathing_rate_bpm, breathing_rate_confidence, fps, num_frames,
            face_detection_rate
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            username,
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


def delete_session(session_id: int, username: str) -> bool:
    conn = get_connection()
    cur = conn.execute(
        "DELETE FROM sessions WHERE id = ? AND username = ?",
        (session_id, username),
    )
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def get_sessions(username: str) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM sessions WHERE username = ? ORDER BY created_at ASC",
        (username,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]
