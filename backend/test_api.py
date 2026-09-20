"""
End-to-end test: hit the real FastAPI app (in-process, via TestClient) with a
synthetic video file that has an actual detectable face-like pattern drawn on
it, and confirm the whole request -> processing -> storage -> response path
works. Uses a temporary DB file so it doesn't pollute the real vitals.db.

Sessions belong to a real signed-in account now (auth.py), not a typed
username - every session-scoped test signs up a throwaway account first and
reuses the TestClient's cookie jar, exactly like a real browser would.
"""

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Point the app at throwaway DB + session-signing-secret files so this
    # test run can't read/write real local state.
    import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test_vitals.db")

    import auth as auth_module
    monkeypatch.setattr(auth_module, "_SECRET_KEY_PATH", tmp_path / "test_session_secret")

    import main
    with TestClient(main.app) as c:
        yield c


def signup(client, username="test_user", password="correct horse battery"):
    """Sign up a throwaway account; the TestClient keeps the session cookie
    for subsequent requests, exactly like a real browser would."""
    response = client.post("/api/auth/signup", data={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def _draw_face_like_frame(width, height, cx, cy, face_radius, green_value):
    """Draw a simple synthetic 'face': a filled circle with two eye-like dark
    circles and a mouth arc. Haar cascades key off contrast patterns like this
    reasonably often for large, well-centered, high-contrast synthetic faces,
    though real-world accuracy obviously depends on real facial features."""
    frame = np.full((height, width, 3), 80, dtype=np.uint8)
    color = (60, int(np.clip(green_value, 0, 255)), 150)  # BGR skin-ish tone, green channel carries our signal
    cv2.circle(frame, (cx, cy), face_radius, color, -1)
    eye_offset_x = face_radius // 2
    eye_offset_y = face_radius // 4
    eye_r = max(face_radius // 8, 3)
    cv2.circle(frame, (cx - eye_offset_x, cy - eye_offset_y), eye_r, (10, 10, 10), -1)
    cv2.circle(frame, (cx + eye_offset_x, cy - eye_offset_y), eye_r, (10, 10, 10), -1)
    cv2.ellipse(frame, (cx, cy + face_radius // 2), (face_radius // 2, face_radius // 4), 0, 0, 180, (10, 10, 10), 3)
    return frame


def _write_synthetic_clip(path, fps=30, duration_s=8, bpm=75):
    width, height = 320, 240
    n_frames = int(fps * duration_s)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(path, fourcc, fps, (width, height))

    freq_hz = bpm / 60.0
    for i in range(n_frames):
        t = i / fps
        green = 150 + 10 * np.sin(2 * np.pi * freq_hz * t)
        frame = _draw_face_like_frame(width, height, width // 2, height // 2, 70, green)
        writer.write(frame)
    writer.release()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_signup_creates_account_and_signs_in(client):
    body = signup(client, "new_user", "correct horse battery")
    assert body["username"] == "new_user"
    assert "id" in body

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "new_user"


def test_signup_rejects_duplicate_username(client):
    signup(client, "taken_user")
    dupe = client.post("/api/auth/signup", data={"username": "taken_user", "password": "another password"})
    assert dupe.status_code == 409


def test_signup_rejects_short_password(client):
    response = client.post("/api/auth/signup", data={"username": "short_pw", "password": "short"})
    assert response.status_code == 422


def test_login_with_correct_password_succeeds(client):
    signup(client, "login_user", "correct horse battery")
    client.post("/api/auth/logout")

    login = client.post("/api/auth/login", data={"username": "login_user", "password": "correct horse battery"})
    assert login.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_login_with_wrong_password_fails(client):
    signup(client, "login_user2", "correct horse battery")
    client.post("/api/auth/logout")

    login = client.post("/api/auth/login", data={"username": "login_user2", "password": "wrong password"})
    assert login.status_code == 401


def test_logout_clears_session(client):
    signup(client, "logout_user")
    assert client.get("/api/auth/me").status_code == 200

    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_session_endpoints_require_auth(client):
    assert client.get("/api/sessions").status_code == 401
    assert client.delete("/api/sessions/1").status_code == 401


# ---------------------------------------------------------------------------
# Session upload / history, now scoped to a real account
# ---------------------------------------------------------------------------

def test_upload_endpoint_processes_synthetic_clip_and_stores_session(client, tmp_path):
    signup(client, "test_user")
    clip_path = str(tmp_path / "clip.mp4")
    _write_synthetic_clip(clip_path, bpm=75)

    with open(clip_path, "rb") as f:
        response = client.post(
            "/api/sessions/upload",
            files={"video": ("clip.mp4", f, "video/mp4")},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "heart_rate_bpm" in body
    assert "breathing_rate_bpm" in body
    assert body["num_frames"] > 0

    history = client.get("/api/sessions")
    assert history.status_code == 200
    sessions = history.json()
    assert len(sessions) == 1
    assert sessions[0]["heart_rate_bpm"] == body["heart_rate_bpm"]


def test_detect_face_endpoint_finds_face_in_synthetic_frame(client):
    frame = _draw_face_like_frame(320, 240, 160, 120, 70, green_value=150)
    ok, encoded = cv2.imencode(".jpg", frame)
    assert ok

    response = client.post(
        "/api/detect-face",
        files={"frame": ("frame.jpg", encoded.tobytes(), "image/jpeg")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["detected"] is True
    regions = body["regions"]
    for key in ("face_bbox", "forehead_roi_bbox", "chest_roi_bbox"):
        box = regions[key]
        assert len(box) == 4
        assert box[2] > 0 and box[3] > 0
    assert regions["frame_width"] == 320
    assert regions["frame_height"] == 240


def test_detect_face_endpoint_reports_no_face_on_blank_frame(client):
    blank = np.full((240, 320, 3), 100, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", blank)
    assert ok

    response = client.post(
        "/api/detect-face",
        files={"frame": ("frame.jpg", encoded.tobytes(), "image/jpeg")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["detected"] is False
    assert body["regions"] is None


def test_upload_endpoint_dev_mode_returns_tracking_and_signal_debug_data(client, tmp_path):
    signup(client, "dev_user")
    clip_path = str(tmp_path / "clip.mp4")
    _write_synthetic_clip(clip_path, bpm=75, duration_s=8)

    with open(clip_path, "rb") as f:
        response = client.post(
            "/api/sessions/upload",
            files={"video": ("clip.mp4", f, "video/mp4")},
            data={"dev_mode": "true"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "debug" in body
    debug = body["debug"]

    n = body["num_frames"]
    assert len(debug["face_bboxes"]) == n
    assert len(debug["forehead_roi_bboxes"]) == n
    assert len(debug["chest_roi_bboxes"]) == n
    assert len(debug["face_detected"]) == n
    assert len(debug["frame_times_s"]) == n
    assert len(debug["green_signal_raw"]) == n
    assert len(debug["green_signal_filtered"]) == n

    # every forehead/chest ROI box is a well-formed [x, y, w, h] with positive area
    for box in debug["forehead_roi_bboxes"] + debug["chest_roi_bboxes"]:
        assert box[2] > 0 and box[3] > 0

    # dev_mode defaults to off and must not add this payload
    with open(clip_path, "rb") as f:
        plain_response = client.post(
            "/api/sessions/upload",
            files={"video": ("clip.mp4", f, "video/mp4")},
        )
    assert "debug" not in plain_response.json()


def test_delete_session_removes_it_from_history(client, tmp_path):
    signup(client, "delete_user")
    clip_path = str(tmp_path / "clip.mp4")
    _write_synthetic_clip(clip_path, bpm=70, duration_s=8)

    with open(clip_path, "rb") as f:
        upload = client.post("/api/sessions/upload", files={"video": ("clip.mp4", f, "video/mp4")})
    session_id = upload.json()["id"]

    delete_response = client.delete(f"/api/sessions/{session_id}")
    assert delete_response.status_code == 200

    history = client.get("/api/sessions")
    assert history.json() == []


def test_delete_session_requires_owning_account(client, tmp_path):
    signup(client, "owner_user")
    clip_path = str(tmp_path / "clip.mp4")
    _write_synthetic_clip(clip_path, bpm=70, duration_s=8)

    with open(clip_path, "rb") as f:
        upload = client.post("/api/sessions/upload", files={"video": ("clip.mp4", f, "video/mp4")})
    session_id = upload.json()["id"]

    # Switch accounts (a fresh signup overwrites the client's session cookie).
    signup(client, "someone_else")
    delete_response = client.delete(f"/api/sessions/{session_id}")
    assert delete_response.status_code == 404

    client.post("/api/auth/logout")
    client.post("/api/auth/login", data={"username": "owner_user", "password": "correct horse battery"})
    history = client.get("/api/sessions")
    assert len(history.json()) == 1


def test_upload_endpoint_rejects_too_short_clip(client, tmp_path):
    signup(client, "test_user")
    clip_path = str(tmp_path / "short.mp4")
    _write_synthetic_clip(clip_path, duration_s=1, bpm=75)

    with open(clip_path, "rb") as f:
        response = client.post(
            "/api/sessions/upload",
            files={"video": ("short.mp4", f, "video/mp4")},
        )

    assert response.status_code == 422


def test_health_endpoint(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
