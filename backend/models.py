"""SQLAlchemy ORM models - the normalized schema that replaced a single
hand-rolled `sessions` table with raw sqlite3.

- `User` / `RecordingSession`: one-to-many, a user's recorded readings.
- `RecordingSession` / `Tag`: many-to-many through `session_tags`, so a
  reading can carry more than one label ("resting", "post-workout") and a
  label can apply to more than one reading - a real relationship, not a
  comma-joined string column standing in for one.
- `Job`: the background-processing queue video upload runs through (see
  main.py) - independent of the above; a job's `result` only becomes a
  `RecordingSession` row once processing finishes and, if the uploader was
  signed in, gets saved to their history.
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


session_tags = Table(
    "session_tags",
    Base.metadata,
    Column("session_id", Integer, ForeignKey("recording_sessions.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String, nullable=False, unique=True)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    sessions = relationship("RecordingSession", back_populates="user", cascade="all, delete-orphan")
    tags = relationship("Tag", back_populates="user", cascade="all, delete-orphan")


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_tag_user_name"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)

    user = relationship("User", back_populates="tags")
    sessions = relationship("RecordingSession", secondary=session_tags, back_populates="tags")


class RecordingSession(Base):
    __tablename__ = "recording_sessions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    heart_rate_bpm = Column(Float, nullable=False)
    heart_rate_confidence = Column(Float, nullable=False)
    breathing_rate_bpm = Column(Float, nullable=False)
    breathing_rate_confidence = Column(Float, nullable=False)
    fps = Column(Float, nullable=True)
    num_frames = Column(Integer, nullable=True)
    face_detection_rate = Column(Float, nullable=True)

    user = relationship("User", back_populates="sessions")
    tags = relationship("Tag", secondary=session_tags, back_populates="sessions")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    # No FK/ondelete here on purpose: an anonymous upload's job has no user
    # at all (user_id is just NULL, never a dangling reference), and a
    # signed-in upload's job is a transient polling record, not a durable
    # relationship worth cascading - deleting the account shouldn't be
    # blocked by, or need to clean up, a job that finished polling minutes
    # or days ago.
    user_id = Column(Integer, nullable=True)
    status = Column(String, nullable=False, default="pending")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    result_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
