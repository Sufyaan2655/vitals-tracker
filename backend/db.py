"""Persistence layer: SQLAlchemy ORM over whatever DATABASE_URL points at
(see database.py) - Postgres in production, SQLite for local dev/tests.

Every function here takes a `db: Session` as its first argument rather than
opening its own connection - FastAPI hands one session per request through
`Depends(get_db)` (see main.py), and tests do the same via a fixture. Read
functions return plain dicts (not ORM objects) so callers - and the JSON
responses built from them - don't have to know or care that SQLAlchemy is
involved at all.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Job, RecordingSession, Tag, User
from vitals import (
    BREATHING_RATE_PLAUSIBLE_BPM,
    HEART_RATE_PLAUSIBLE_BPM,
    is_plausible_bpm,
)


class UsernameTakenError(Exception):
    """Raised by create_user() when the username is already registered."""


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(db: Session, username: str, password_hash: str) -> int:
    user = User(username=username, password_hash=password_hash)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise UsernameTakenError(username) from None
    db.refresh(user)
    return user.id


def get_user_by_username(db: Session, username: str) -> dict | None:
    user = db.scalar(select(User).where(User.username == username))
    return _user_to_dict(user) if user else None


def get_user(db: Session, user_id: int) -> dict | None:
    user = db.get(User, user_id)
    return _user_to_dict(user) if user else None


def _user_to_dict(user: User) -> dict:
    return {"id": user.id, "username": user.username, "password_hash": user.password_hash}


# ---------------------------------------------------------------------------
# Recording sessions
# ---------------------------------------------------------------------------

def insert_session(db: Session, user_id: int, result: dict) -> int:
    session = RecordingSession(
        user_id=user_id,
        heart_rate_bpm=result["heart_rate_bpm"],
        heart_rate_confidence=result["heart_rate_confidence"],
        breathing_rate_bpm=result["breathing_rate_bpm"],
        breathing_rate_confidence=result["breathing_rate_confidence"],
        fps=result.get("fps"),
        num_frames=result.get("num_frames"),
        face_detection_rate=result.get("face_detection_rate"),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session.id


def delete_session(db: Session, session_id: int, user_id: int) -> bool:
    session = db.scalar(
        select(RecordingSession).where(RecordingSession.id == session_id, RecordingSession.user_id == user_id)
    )
    if session is None:
        return False
    db.delete(session)
    db.commit()
    return True


def get_sessions(db: Session, user_id: int) -> list[dict]:
    sessions = db.scalars(
        select(RecordingSession)
        .where(RecordingSession.user_id == user_id)
        .order_by(RecordingSession.created_at.asc())
    ).all()
    return [_session_to_dict(s) for s in sessions]


def _session_to_dict(session: RecordingSession) -> dict:
    return {
        "id": session.id,
        "user_id": session.user_id,
        "created_at": session.created_at.timestamp() if session.created_at else None,
        "heart_rate_bpm": session.heart_rate_bpm,
        "heart_rate_confidence": session.heart_rate_confidence,
        "breathing_rate_bpm": session.breathing_rate_bpm,
        "breathing_rate_confidence": session.breathing_rate_confidence,
        "fps": session.fps,
        "num_frames": session.num_frames,
        "face_detection_rate": session.face_detection_rate,
        # Plausibility isn't a stored column - it's derived from bpm at read
        # time so historical sessions (recorded before this check existed,
        # or scored against a threshold that later changes) always reflect
        # the current definition instead of a stale flag frozen at insert
        # time.
        "heart_rate_plausible": is_plausible_bpm(session.heart_rate_bpm, HEART_RATE_PLAUSIBLE_BPM),
        "breathing_rate_plausible": is_plausible_bpm(session.breathing_rate_bpm, BREATHING_RATE_PLAUSIBLE_BPM),
        "tags": sorted(t.name for t in session.tags),
    }


# ---------------------------------------------------------------------------
# Tags - a many-to-many label a user can attach to their own sessions
# ("resting", "post-workout", "morning") to make their history filterable
# by context later, not just by time.
# ---------------------------------------------------------------------------

def add_tag(db: Session, session_id: int, user_id: int, tag_name: str) -> list[str] | None:
    """Attach `tag_name` to a session the user owns, creating that tag for
    the user if it doesn't already exist. Returns the session's updated tag
    list, or None if the session isn't found (or isn't theirs)."""
    session = db.scalar(
        select(RecordingSession).where(RecordingSession.id == session_id, RecordingSession.user_id == user_id)
    )
    if session is None:
        return None

    tag = db.scalar(select(Tag).where(Tag.user_id == user_id, Tag.name == tag_name))
    if tag is None:
        tag = Tag(user_id=user_id, name=tag_name)
        db.add(tag)

    if tag not in session.tags:
        session.tags.append(tag)
    db.commit()
    db.refresh(session)
    return sorted(t.name for t in session.tags)


def remove_tag(db: Session, session_id: int, user_id: int, tag_name: str) -> list[str] | None:
    session = db.scalar(
        select(RecordingSession).where(RecordingSession.id == session_id, RecordingSession.user_id == user_id)
    )
    if session is None:
        return None
    session.tags = [t for t in session.tags if t.name != tag_name]
    db.commit()
    db.refresh(session)
    return sorted(t.name for t in session.tags)


def list_tags(db: Session, user_id: int) -> list[str]:
    """Every tag name this user has ever created, regardless of whether it's
    currently attached to a session - powers a suggestion list in the UI
    rather than requiring someone to retype "post-workout" exactly the same
    way every time."""
    names = db.scalars(select(Tag.name).where(Tag.user_id == user_id).order_by(Tag.name)).all()
    return list(names)


# ---------------------------------------------------------------------------
# Background job queue: video processing is the compute-heavy part of this
# app, so the upload endpoint creates a row here and returns immediately
# rather than blocking the HTTP request for the duration of processing. A
# FastAPI BackgroundTask (see main.py) does the actual work and updates the
# row through the same three states every job passes through once: pending
# -> processing -> done (with a result) or failed (with an error message).
# ---------------------------------------------------------------------------

def create_job(db: Session, user_id: int | None) -> int:
    job = Job(user_id=user_id, status="pending")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job.id


def get_job(db: Session, job_id: int) -> dict | None:
    job = db.get(Job, job_id)
    return _job_to_dict(job) if job else None


def update_job(
    db: Session,
    job_id: int,
    status: str,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    job.status = status
    job.result_json = json.dumps(result) if result is not None else None
    job.error = error
    db.commit()


def _job_to_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "user_id": job.user_id,
        "status": job.status,
        "created_at": job.created_at.timestamp() if job.created_at else None,
        "updated_at": job.updated_at.timestamp() if job.updated_at else None,
        "result": json.loads(job.result_json) if job.result_json else None,
        "error": job.error,
    }
