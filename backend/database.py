"""Engine/session setup - reads DATABASE_URL from the environment so the
same code runs against Postgres in production (Vercel Postgres/Neon,
Railway, etc.) and SQLite for local dev/tests without any code change,
just a different connection string.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_SQLITE_URL = f"sqlite:///{Path(__file__).parent / 'vitals.db'}"

DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_SQLITE_URL)

# Managed Postgres providers (Heroku, Vercel Postgres/Neon, Railway) commonly
# hand out a connection string starting "postgres://", which SQLAlchemy 2.x
# no longer accepts - it wants the "postgresql://" form. Normalizing here
# means the DATABASE_URL a provider gives you can be pasted in unmodified.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

# SQLite's driver rejects a connection being used from more than the thread
# that created it by default, which conflicts with how FastAPI's dependency
# injection can hand the same request off across threads/tasks; Postgres has
# no such restriction. Only SQLite needs this flag.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    """FastAPI dependency: one session per request, always closed after."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
