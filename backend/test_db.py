"""Tests for db.py's schema migrations - specifically, that init_db()
correctly upgrades a database created under an older schema version rather
than assuming every database on disk already matches the current
CREATE TABLE statements (which only apply to a table that doesn't exist yet;
CREATE TABLE IF NOT EXISTS is a no-op against one that already does).
"""

import sqlite3

import pytest


@pytest.fixture()
def db_module(tmp_path, monkeypatch):
    import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test_vitals.db")
    return db_module


def _create_pre_accounts_sessions_table(db_path):
    """Build a `sessions` table matching the schema from before real
    accounts existed: `username TEXT NOT NULL`, no `user_id` column at
    all - the exact shape a database created early in this project's life
    would still have on disk, since ALTER TABLE ADD COLUMN migrations only
    ever added to that shape, never corrected the column it left behind."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE sessions (
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


def test_init_db_drops_stale_username_not_null_column(db_module):
    """Regression test: a database that predates `user_id` still enforces
    `username NOT NULL` even after upgrading, because CREATE TABLE IF NOT
    EXISTS never touches an existing table. insert_session() stopped
    supplying a username long ago, so every save used to fail with
    "NOT NULL constraint failed: sessions.username" - reproduced here by
    building that exact stale schema, running init_db(), and confirming a
    session can actually be saved afterward."""
    _create_pre_accounts_sessions_table(db_module.DB_PATH)

    db_module.init_db()

    conn = sqlite3.connect(db_module.DB_PATH)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    conn.close()
    assert "username" not in cols
    assert "user_id" in cols

    user_id = db_module.create_user("migrated_user", "not-a-real-hash")
    session_id = db_module.insert_session(
        user_id,
        {
            "heart_rate_bpm": 70.0,
            "heart_rate_confidence": 0.9,
            "breathing_rate_bpm": 14.0,
            "breathing_rate_confidence": 0.8,
            "fps": 30.0,
            "num_frames": 450,
            "face_detection_rate": 1.0,
        },
    )
    assert session_id is not None
    sessions = db_module.get_sessions(user_id)
    assert len(sessions) == 1
    assert sessions[0]["heart_rate_bpm"] == 70.0


def test_init_db_is_idempotent_on_an_already_current_schema(db_module):
    """init_db() must be safe to call on every startup (it is - see
    main.py's lifespan) regardless of whether the database is brand new,
    already migrated, or - after this fix - already missing `username`."""
    db_module.init_db()
    db_module.init_db()  # must not raise on a schema that's already current

    conn = sqlite3.connect(db_module.DB_PATH)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    conn.close()
    assert "username" not in cols
    assert "user_id" in cols


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
