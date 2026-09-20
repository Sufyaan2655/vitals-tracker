"""
End-to-end test: hit the real FastAPI app (in-process, via TestClient) with a
synthetic video file that has an actual detectable face-like pattern drawn on
it, and confirm the whole request -> processing -> storage -> response path
works. Uses a temporary DB file so it doesn't pollute the real vitals.db.
"""

import os
import tempfile

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Point the app at a throwaway DB for this test run.
    import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test_vitals.db")

    import main
    with TestClient(main.app) as c:
        yield c


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


def test_upload_endpoint_processes_synthetic_clip_and_stores_session(client, tmp_path):
    clip_path = str(tmp_path / "clip.mp4")
    _write_synthetic_clip(clip_path, bpm=75)

    with open(clip_path, "rb") as f:
        response = client.post(
            "/api/sessions/upload",
            files={"video": ("clip.mp4", f, "video/mp4")},
            data={"username": "test_user"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "heart_rate_bpm" in body
    assert "breathing_rate_bpm" in body
    assert body["username"] == "test_user"
    assert body["num_frames"] > 0

    history = client.get("/api/sessions", params={"username": "test_user"})
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
    clip_path = str(tmp_path / "clip.mp4")
    _write_synthetic_clip(clip_path, bpm=75, duration_s=8)

    with open(clip_path, "rb") as f:
        response = client.post(
            "/api/sessions/upload",
            files={"video": ("clip.mp4", f, "video/mp4")},
            data={"username": "dev_user", "dev_mode": "true"},
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
            data={"username": "dev_user"},
        )
    assert "debug" not in plain_response.json()


def test_delete_session_removes_it_from_history(client, tmp_path):
    clip_path = str(tmp_path / "clip.mp4")
    _write_synthetic_clip(clip_path, bpm=70, duration_s=8)

    with open(clip_path, "rb") as f:
        upload = client.post(
            "/api/sessions/upload",
            files={"video": ("clip.mp4", f, "video/mp4")},
            data={"username": "delete_user"},
        )
    session_id = upload.json()["id"]

    delete_response = client.delete(f"/api/sessions/{session_id}", params={"username": "delete_user"})
    assert delete_response.status_code == 200

    history = client.get("/api/sessions", params={"username": "delete_user"})
    assert history.json() == []


def test_delete_session_requires_matching_username(client, tmp_path):
    clip_path = str(tmp_path / "clip.mp4")
    _write_synthetic_clip(clip_path, bpm=70, duration_s=8)

    with open(clip_path, "rb") as f:
        upload = client.post(
            "/api/sessions/upload",
            files={"video": ("clip.mp4", f, "video/mp4")},
            data={"username": "owner_user"},
        )
    session_id = upload.json()["id"]

    delete_response = client.delete(f"/api/sessions/{session_id}", params={"username": "someone_else"})
    assert delete_response.status_code == 404

    history = client.get("/api/sessions", params={"username": "owner_user"})
    assert len(history.json()) == 1


def test_upload_endpoint_rejects_too_short_clip(client, tmp_path):
    clip_path = str(tmp_path / "short.mp4")
    _write_synthetic_clip(clip_path, duration_s=1, bpm=75)

    with open(clip_path, "rb") as f:
        response = client.post(
            "/api/sessions/upload",
            files={"video": ("short.mp4", f, "video/mp4")},
            data={"username": "test_user"},
        )

    assert response.status_code == 422


def test_health_endpoint(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
