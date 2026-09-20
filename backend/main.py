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
from vitals import process_video, VitalsError


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
async def upload_session(video: UploadFile, username: str = Form(...)):
    username = username.strip() or "anonymous"

    suffix = Path(video.filename or "clip.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(video.file, tmp)
        tmp_path = tmp.name

    try:
        result = process_video(tmp_path)
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


@app.get("/api/sessions")
def list_sessions(username: str):
    return db.get_sessions(username)


FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
