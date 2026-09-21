"""
FastAPI backend for the personal vital-signs tracker.

Endpoints:
  POST /api/auth/signup       - create an account, signs in
  POST /api/auth/login        - sign in
  POST /api/auth/logout       - sign out
  GET  /api/auth/me           - who's signed in (401 if nobody)
  POST /api/sessions/upload   - upload a short webcam clip. Returns a job id
                                 immediately (processing runs in the
                                 background - see the jobs queue below)
                                 rather than blocking the request. Works
                                 signed-out (result just isn't saved) or
                                 signed-in (saved to that account's history) -
                                 trying the tool never requires an account.
  GET  /api/jobs/{job_id}     - poll a processing job's status/result
  GET  /api/sessions          - the signed-in user's session history (for the trends chart)
  POST /api/sessions/{id}/tags   - attach a tag to one of your sessions
  DELETE /api/sessions/{id}/tags/{tag_name} - remove a tag from one of your sessions
  GET  /api/tags              - every tag name you've ever used (for autocomplete)
  GET  /api/health            - basic liveness check

Sessions belong to a real account (see auth.py) - a signed, HttpOnly cookie,
not a typed username string. Serves the frontend (frontend/index.html +
app.js + style.css) as static files from "/", so the whole app runs from a
single origin at http://localhost:8000 - this matters because getUserMedia
(camera access) requires a "secure context", and browsers treat
http://localhost as secure even without TLS, so no separate HTTPS setup is
needed for local use.

Persistence is SQLAlchemy over DATABASE_URL (see database.py) - Postgres in
production, SQLite for local dev. Every endpoint that touches the database
takes `session: Session = Depends(get_db)`, one session per request.
"""

import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from database import SessionLocal, engine, get_db
from models import Base
from auth import (
    SESSION_MAX_AGE_SECONDS,
    create_session_token,
    hash_password,
    verify_password,
    verify_session_token,
)
from vitals import process_video, VitalsError, detect_face_regions_from_bytes

SESSION_COOKIE = "session"
MIN_PASSWORD_LENGTH = 8


@asynccontextmanager
async def lifespan(app: FastAPI):
    # In production this schema is owned by Alembic migrations (see
    # backend/alembic/ - run `alembic upgrade head` as a deploy step); this
    # call is a local-dev convenience so `uvicorn main:app --reload` keeps
    # working against a throwaway SQLite file with zero setup. It's a no-op
    # against a database Alembic already migrated (create_all only creates
    # tables that don't exist yet, never alters ones that do), so it's safe
    # to leave in for both cases rather than branching on environment.
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Vitals Tracker", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_frontend(request, call_next):
    # This app is under active local iteration - browsers otherwise cache
    # index.html/app.js/style.css heuristically (no explicit Cache-Control
    # from StaticFiles) and a plain refresh can keep serving a stale UI even
    # after the server has the new files. Force revalidation on every load.
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Starlette's own default 500 handler returns plain text, not JSON - the
    # frontend's res.json() then throws its own confusing "Unexpected token"
    # parse error instead of surfacing whatever actually broke. Any exception
    # that reaches here is a real bug (every known failure mode is already
    # caught and turned into an HTTPException closer to its source), but the
    # response format should stay JSON regardless of where it came from.
    return JSONResponse(status_code=500, content={"detail": f"Unexpected server error: {exc}"})


def get_current_user_id(session: str | None = Cookie(default=None)) -> int:
    if session is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    user_id = verify_session_token(session)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Session expired - please sign in again")
    return user_id


def get_current_user_id_optional(session: str | None = Cookie(default=None)) -> int | None:
    # Recording a clip and seeing your result works without an account -
    # signing in is what turns a one-off reading into saved history, not a
    # requirement to use the tool at all. An invalid/expired cookie is
    # treated the same as no cookie here (anonymous), not an error - only
    # endpoints that actually need an account (history, delete) use the
    # hard-requiring get_current_user_id instead.
    if session is None:
        return None
    return verify_session_token(session)


def _set_session_cookie(response: Response, request: Request, user_id: int) -> None:
    # `secure` is conditional, not hardcoded True: this app also runs over
    # plain http://localhost for local dev (browsers treat localhost as a
    # secure context for getUserMedia regardless), where a Secure cookie
    # would simply never be sent back. Once deployed behind real HTTPS,
    # request.url.scheme flips to "https" and this starts protecting the
    # cookie in transit automatically - no code change needed at deploy time.
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(user_id),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=SESSION_MAX_AGE_SECONDS,
    )


@app.post("/api/auth/signup")
def signup(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    db_session: Session = Depends(get_db),
):
    username = username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="Choose a username")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=422, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    try:
        user_id = db.create_user(db_session, username, hash_password(password))
    except db.UsernameTakenError:
        raise HTTPException(status_code=409, detail="That username is already taken") from None
    _set_session_cookie(response, request, user_id)
    return {"id": user_id, "username": username}


@app.post("/api/auth/login")
def login(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    db_session: Session = Depends(get_db),
):
    user = db.get_user_by_username(db_session, username.strip())
    if user is None or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    _set_session_cookie(response, request, user["id"])
    return {"id": user["id"], "username": user["username"]}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user_id: int = Depends(get_current_user_id), db_session: Session = Depends(get_db)):
    user = db.get_user(db_session, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return {"id": user["id"], "username": user["username"]}


def _run_processing_job(job_id: int, tmp_path: str, dev_mode: bool, user_id: int | None) -> None:
    # Runs off the request thread (see BackgroundTasks below) - video
    # processing (face detection, band-pass filtering, FFT) is the
    # compute-heavy part of this app, and used to block the whole upload
    # request for its duration. The job row is how the frontend finds out
    # what happened instead of waiting on the response.
    #
    # This runs outside any request's dependency-injection scope, so it
    # can't reuse Depends(get_db) - it opens and closes its own session for
    # its own lifetime, the same one-per-unit-of-work pattern, just not
    # tied to an HTTP request.
    #
    # Everything from here to the final update_job() is inside one try -
    # this used to end at process_video(), which left the save-to-history
    # step unprotected: if insert_session() ever raised (a locked DB, any
    # error at all), the job never got marked done *or* failed and sat at
    # "processing" forever, and the frontend's poll loop would wait out its
    # full timeout and report a generic "taking longer than expected" with
    # no hint that a signed-in-only code path was the actual cause.
    db_session = SessionLocal()
    try:
        db.update_job(db_session, job_id, status="processing")
        try:
            try:
                result = process_video(tmp_path, include_debug=dev_mode)
            finally:
                Path(tmp_path).unlink(missing_ok=True)

            # Signed in -> save to that account's history. Anonymous -> just
            # hand back the reading for this one clip; nothing is written to
            # the DB, so there's nothing to clean up or associate with an
            # account later.
            if user_id is not None:
                result["id"] = db.insert_session(db_session, user_id, result)
            else:
                result["id"] = None
            result["saved"] = user_id is not None
        except VitalsError as e:
            db.update_job(db_session, job_id, status="failed", error=str(e))
            return
        except Exception as e:  # noqa: BLE001 - surface unexpected processing/storage errors to the client
            db.update_job(db_session, job_id, status="failed", error=f"Processing failed: {e}")
            return

        db.update_job(db_session, job_id, status="done", result=result)
    finally:
        db_session.close()


@app.post("/api/sessions/upload")
async def upload_session(
    background_tasks: BackgroundTasks,
    video: UploadFile,
    dev_mode: bool = Form(False),
    user_id: int | None = Depends(get_current_user_id_optional),
    db_session: Session = Depends(get_db),
):
    suffix = Path(video.filename or "clip.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(video.file, tmp)
        tmp_path = tmp.name

    job_id = db.create_job(db_session, user_id)
    background_tasks.add_task(_run_processing_job, job_id, tmp_path, dev_mode, user_id)
    return {"job_id": job_id, "status": "pending"}


@app.get("/api/jobs/{job_id}")
def get_job_status(
    job_id: int,
    user_id: int | None = Depends(get_current_user_id_optional),
    db_session: Session = Depends(get_db),
):
    job = db.get_job(db_session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # A job created anonymously has no owner to check against - polling it
    # is the immediate follow-up to the upload that created it, the same
    # trust boundary the old synchronous response had (no auth needed to
    # read back your own just-created result). A job created by a signed-in
    # user is scoped to that account like everything else.
    if job["user_id"] is not None and job["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/detect-face")
async def detect_face(frame: UploadFile):
    # Lightweight, stateless single-frame detection for the live dev-mode
    # preview overlay - deliberately reuses the exact same detector +
    # ROI math as the real pipeline (see detect_face_regions_from_bytes)
    # instead of a separate client-side detector, so what's shown live
    # can't drift from what a recorded clip would actually be scored on.
    contents = await frame.read()
    try:
        regions = detect_face_regions_from_bytes(contents)
    except VitalsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"detected": regions is not None, "regions": regions}


@app.get("/api/sessions")
def list_sessions(user_id: int = Depends(get_current_user_id), db_session: Session = Depends(get_db)):
    return db.get_sessions(db_session, user_id)


@app.delete("/api/sessions/{session_id}")
def delete_session(
    session_id: int,
    user_id: int = Depends(get_current_user_id),
    db_session: Session = Depends(get_db),
):
    deleted = db.delete_session(db_session, session_id, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": session_id}


@app.post("/api/sessions/{session_id}/tags")
def add_session_tag(
    session_id: int,
    name: str = Form(...),
    user_id: int = Depends(get_current_user_id),
    db_session: Session = Depends(get_db),
):
    name = name.strip().lower()
    if not name:
        raise HTTPException(status_code=422, detail="Tag can't be empty")
    tags = db.add_tag(db_session, session_id, user_id, name)
    if tags is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"tags": tags}


@app.delete("/api/sessions/{session_id}/tags/{name}")
def remove_session_tag(
    session_id: int,
    name: str,
    user_id: int = Depends(get_current_user_id),
    db_session: Session = Depends(get_db),
):
    tags = db.remove_tag(db_session, session_id, user_id, name)
    if tags is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"tags": tags}


@app.get("/api/tags")
def list_tags(user_id: int = Depends(get_current_user_id), db_session: Session = Depends(get_db)):
    return {"tags": db.list_tags(db_session, user_id)}


FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
