"""
FastAPI backend for the personal vital-signs tracker.

Endpoints:
  POST /api/auth/signup       - create an account, signs in
  POST /api/auth/login        - sign in
  POST /api/auth/logout       - sign out
  GET  /api/auth/me           - who's signed in (401 if nobody)
  POST /api/sessions/upload   - upload a short webcam clip, get back HR/BR estimates
  GET  /api/sessions          - the signed-in user's session history (for the trends chart)
  GET  /api/health            - basic liveness check

Sessions belong to a real account (see auth.py) - a signed, HttpOnly cookie,
not a typed username string. Serves the frontend (frontend/index.html +
app.js + style.css) as static files from "/", so the whole app runs from a
single origin at http://localhost:8000 - this matters because getUserMedia
(camera access) requires a "secure context", and browsers treat
http://localhost as secure even without TLS, so no separate HTTPS setup is
needed for local use.
"""

import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import db
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
    db.init_db()
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


def get_current_user_id(session: str | None = Cookie(default=None)) -> int:
    if session is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    user_id = verify_session_token(session)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Session expired - please sign in again")
    return user_id


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
def signup(request: Request, response: Response, username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="Choose a username")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=422, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    try:
        user_id = db.create_user(username, hash_password(password))
    except db.UsernameTakenError:
        raise HTTPException(status_code=409, detail="That username is already taken") from None
    _set_session_cookie(response, request, user_id)
    return {"id": user_id, "username": username}


@app.post("/api/auth/login")
def login(request: Request, response: Response, username: str = Form(...), password: str = Form(...)):
    user = db.get_user_by_username(username.strip())
    if user is None or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    _set_session_cookie(response, request, user["id"])
    return {"id": user["id"], "username": user["username"]}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user_id: int = Depends(get_current_user_id)):
    user = db.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return {"id": user["id"], "username": user["username"]}


@app.post("/api/sessions/upload")
async def upload_session(
    video: UploadFile,
    dev_mode: bool = Form(False),
    user_id: int = Depends(get_current_user_id),
):
    suffix = Path(video.filename or "clip.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(video.file, tmp)
        tmp_path = tmp.name

    try:
        result = process_video(tmp_path, include_debug=dev_mode)
    except VitalsError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:  # noqa: BLE001 - surface unexpected processing errors to the client
        raise HTTPException(status_code=500, detail=f"Processing failed: {e}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    session_id = db.insert_session(user_id, result)
    result["id"] = session_id
    return result


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
def list_sessions(user_id: int = Depends(get_current_user_id)):
    return db.get_sessions(user_id)


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: int, user_id: int = Depends(get_current_user_id)):
    deleted = db.delete_session(session_id, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": session_id}


FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
