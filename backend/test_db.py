"""Tests for db.py's SQLAlchemy-based CRUD functions, independent of the
HTTP layer (see test_api.py for the end-to-end path). Each test gets its
own in-memory SQLite database built straight from the ORM models - fast,
and doesn't touch a Postgres server or a real file - so this validates the
CRUD logic itself, not any particular database backend.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db
from models import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _sample_result(**overrides):
    result = {
        "heart_rate_bpm": 70.0,
        "heart_rate_confidence": 0.9,
        "breathing_rate_bpm": 14.0,
        "breathing_rate_confidence": 0.8,
        "fps": 30.0,
        "num_frames": 450,
        "face_detection_rate": 1.0,
    }
    result.update(overrides)
    return result


def test_create_user_then_lookup_by_username_and_id(session):
    user_id = db.create_user(session, "alice", "hashed")
    assert user_id is not None

    by_username = db.get_user_by_username(session, "alice")
    assert by_username["id"] == user_id
    assert by_username["password_hash"] == "hashed"

    by_id = db.get_user(session, user_id)
    assert by_id["username"] == "alice"


def test_create_user_rejects_duplicate_username(session):
    db.create_user(session, "bob", "hash1")
    with pytest.raises(db.UsernameTakenError):
        db.create_user(session, "bob", "hash2")


def test_insert_and_get_sessions_scoped_to_user(session):
    user_id = db.create_user(session, "carol", "hash")
    other_id = db.create_user(session, "dave", "hash")

    db.insert_session(session, user_id, _sample_result(heart_rate_bpm=72.0))
    db.insert_session(session, other_id, _sample_result(heart_rate_bpm=99.0))

    carols = db.get_sessions(session, user_id)
    assert len(carols) == 1
    assert carols[0]["heart_rate_bpm"] == 72.0
    assert carols[0]["tags"] == []


def test_delete_session_only_removes_the_owning_users_row(session):
    user_id = db.create_user(session, "erin", "hash")
    other_id = db.create_user(session, "frank", "hash")
    session_id = db.insert_session(session, user_id, _sample_result())

    assert db.delete_session(session, session_id, other_id) is False
    assert len(db.get_sessions(session, user_id)) == 1

    assert db.delete_session(session, session_id, user_id) is True
    assert db.get_sessions(session, user_id) == []


def test_tags_are_a_real_many_to_many_not_a_comma_joined_string(session):
    user_id = db.create_user(session, "grace", "hash")
    resting_id = db.insert_session(session, user_id, _sample_result(heart_rate_bpm=62.0))
    workout_id = db.insert_session(session, user_id, _sample_result(heart_rate_bpm=140.0))

    db.add_tag(session, resting_id, user_id, "resting")
    db.add_tag(session, resting_id, user_id, "morning")
    db.add_tag(session, workout_id, user_id, "post-workout")
    # Same tag name reused across two different sessions for the same user -
    # this only works cleanly if "resting" is one row referenced twice, not
    # two independent copies of the string.
    db.add_tag(session, workout_id, user_id, "resting")

    sessions = {s["id"]: s for s in db.get_sessions(session, user_id)}
    assert sessions[resting_id]["tags"] == ["morning", "resting"]
    assert sessions[workout_id]["tags"] == ["post-workout", "resting"]

    # list_tags reflects the user's whole vocabulary, deduplicated, not one
    # entry per (session, tag) attachment.
    assert db.list_tags(session, user_id) == ["morning", "post-workout", "resting"]


def test_add_tag_is_idempotent_and_scoped_to_the_owning_user(session):
    user_id = db.create_user(session, "heidi", "hash")
    other_id = db.create_user(session, "ivan", "hash")
    session_id = db.insert_session(session, user_id, _sample_result())

    assert db.add_tag(session, session_id, other_id, "resting") is None  # not their session

    db.add_tag(session, session_id, user_id, "resting")
    tags = db.add_tag(session, session_id, user_id, "resting")  # adding it again shouldn't duplicate
    assert tags == ["resting"]


def test_remove_tag(session):
    user_id = db.create_user(session, "judy", "hash")
    session_id = db.insert_session(session, user_id, _sample_result())
    db.add_tag(session, session_id, user_id, "resting")
    db.add_tag(session, session_id, user_id, "morning")

    remaining = db.remove_tag(session, session_id, user_id, "resting")
    assert remaining == ["morning"]


def test_job_lifecycle(session):
    job_id = db.create_job(session, user_id=None)
    job = db.get_job(session, job_id)
    assert job["status"] == "pending"
    assert job["result"] is None

    db.update_job(session, job_id, status="processing")
    assert db.get_job(session, job_id)["status"] == "processing"

    db.update_job(session, job_id, status="done", result=_sample_result())
    done = db.get_job(session, job_id)
    assert done["status"] == "done"
    assert done["result"]["heart_rate_bpm"] == 70.0


def test_get_job_returns_none_for_unknown_id(session):
    assert db.get_job(session, 999999) is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
