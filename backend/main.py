"""
FastAPI backend for the personal vital-signs tracker.

Endpoints:
  POST /api/sessions/upload   - upload a short webcam clip, get back HR/BR estimates
  GET  /api/sessions          - get a user's session history (for the trends chart)
  GET  /api/health            - basic liveness check

Serves the frontend (frontend/index.html + app.js + style.css) as static files
from "/", so the whole app runs from a single origin at http://localhost:8000 -
this matters because getUserMedia (camera access) requires a "secure context",
and browsers treat http://localhost as secure even without TLS, so no separate
HTTPS setup is needed for local use.
"""

import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import db
from vitals import process_video, VitalsError, detect_face_regions_from_bytes


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


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/sessions/upload")
async def upload_session(video: UploadFile, username: str = Form(...), dev_mode: bool = Form(False)):
    username = username.strip() or "anonymous"

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

    session_id = db.insert_session(username, result)
    result["id"] = session_id
    result["username"] = username
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
def list_sessions(username: str):
    return db.get_sessions(username)


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: int, username: str):
    # No real auth (see roadmap) - scope the delete to the matching username,
    # same trust model as everything else here, so one typed name can't be
    # used to guess-delete another name's session by id alone.
    deleted = db.delete_session(session_id, username)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": session_id}


FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
