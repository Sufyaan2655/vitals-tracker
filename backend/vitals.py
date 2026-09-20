"""
Remote photoplethysmography (rPPG) + breathing extraction from a short webcam clip.

Core idea:
- A person's heartbeat causes tiny, invisible-to-the-eye periodic changes in facial
  skin color as blood volume changes under the skin (the same physical principle a
  pulse oximeter uses, just picked up by a normal camera instead of an LED+sensor).
- We track a face region across frames, average its color per frame to build a
  time series, band-pass filter it to the plausible heart-rate range, and find the
  dominant frequency via FFT.
- Breathing is estimated the same way, but from *motion* (chest/shoulder rise and
  fall via optical flow) rather than color, band-passed to the breathing-rate range.

This is deliberately split into small, independently testable pieces:
  - bandpass_filter / dominant_frequency_bpm: pure signal-processing, no video/CV
    involved, testable with synthetic sine waves.
  - extract_signals_from_frames: turns frames + face boxes into raw time series,
    testable with synthetic frames and a fixed bounding box (no face detector
    needed).
  - process_video: the real end-to-end entry point used by the API, which adds
    face detection on top of the above.
"""

from __future__ import annotations

import cv2
import numpy as np
from scipy.signal import butter, filtfilt, detrend

HEART_RATE_BAND_HZ = (0.7, 4.0)   # 42-240 bpm
BREATHING_BAND_HZ = (0.1, 0.6)    # 6-36 breaths/min

_FACE_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
_face_cascade = None


def _get_face_cascade() -> cv2.CascadeClassifier:
    global _face_cascade
    if _face_cascade is None:
        _face_cascade = cv2.CascadeClassifier(_FACE_CASCADE_PATH)
    return _face_cascade


class VitalsError(ValueError):
    """Raised when a video can't be processed (too short, no face found, etc.)."""


# ---------------------------------------------------------------------------
# Pure signal processing (no OpenCV video I/O) - easy to unit test directly.
# ---------------------------------------------------------------------------

def bandpass_filter(signal: np.ndarray, fs: float, low: float, high: float, order: int = 4) -> np.ndarray:
    """Zero-phase Butterworth band-pass filter."""
    signal = np.asarray(signal, dtype=np.float64)
    nyq = 0.5 * fs
    low_n = max(low / nyq, 1e-6)
    high_n = min(high / nyq, 0.999)
    b, a = butter(order, [low_n, high_n], btype="band")
    padlen = 3 * (max(len(a), len(b)) - 1)
    if len(signal) <= padlen:
        # Not enough samples for filtfilt's default padding; fall back to a
        # shorter, still-valid pad length rather than crashing.
        padlen = max(len(signal) - 1, 0)
    return filtfilt(b, a, signal, padlen=padlen)


def dominant_frequency_bpm(signal: np.ndarray, fs: float) -> float:
    """Return the dominant frequency of `signal`, expressed in cycles/minute (bpm)."""
    signal = np.asarray(signal, dtype=np.float64)
    n = len(signal)
    if n < 4:
        raise VitalsError("Signal too short to estimate a frequency")
    windowed = signal * np.hanning(n)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    magnitude = np.abs(np.fft.rfft(windowed))
    magnitude[0] = 0.0  # ignore DC component
    peak_idx = int(np.argmax(magnitude))
    return float(freqs[peak_idx] * 60.0)


def signal_quality(signal: np.ndarray, fs: float, band: tuple[float, float]) -> float:
    """
    Rough confidence score in [0, 1]: fraction of spectral energy inside `band`
    relative to the whole spectrum. A clean periodic signal concentrates almost
    all its energy in a narrow peak; noise spreads energy everywhere.
    """
    signal = np.asarray(signal, dtype=np.float64)
    n = len(signal)
    windowed = signal * np.hanning(n)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    power = np.abs(np.fft.rfft(windowed)) ** 2
    power[0] = 0.0
    total = power.sum()
    if total <= 0:
        return 0.0
    in_band = power[(freqs >= band[0]) & (freqs <= band[1])].sum()
    return float(np.clip(in_band / total, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Frame-level extraction - testable with synthetic frames + a fixed bbox,
# no face detector required.
# ---------------------------------------------------------------------------

def forehead_roi(frame: np.ndarray, bbox: tuple[int, int, int, int]) -> np.ndarray:
    """Crop the forehead sub-region of a face bounding box (most motion-stable,
    least occluded-by-glasses/beard part of the face for rPPG)."""
    x, y, w, h = bbox
    x1, x2 = x + int(w * 0.30), x + int(w * 0.70)
    y1, y2 = y + int(h * 0.05), y + int(h * 0.25)
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        roi = frame[y:y + h, x:x + w]
    return roi


def chest_roi_bounds(frame_shape: tuple[int, int], bbox: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Region below the face used to pick up chest/shoulder motion for breathing."""
    height, width = frame_shape[:2]
    x, y, w, h = bbox
    y1 = min(y + h, height - 2)
    y2 = min(y + int(h * 2.2), height)
    x1 = max(x - int(w * 0.3), 0)
    x2 = min(x + w + int(w * 0.3), width)
    if y2 <= y1:
        y2 = min(y1 + 2, height)
    return y1, y2, x1, x2


def extract_signals_from_frames(
    frames: list[np.ndarray], bboxes: list[tuple[int, int, int, int]]
) -> tuple[np.ndarray, np.ndarray]:
    """
    Given frames and a per-frame face bounding box, build:
      - green_signal: mean green-channel intensity of the forehead ROI per frame
        (the channel with the strongest blood-volume-pulse signal for typical
        RGB cameras).
      - chest_flow_signal: mean vertical optical-flow in the chest region per
        frame (proxy for breathing motion).
    """
    green_signal = np.zeros(len(frames), dtype=np.float64)
    chest_flow_signal = np.zeros(len(frames), dtype=np.float64)

    prev_gray_chest = None
    for i, (frame, bbox) in enumerate(zip(frames, bboxes)):
        roi = forehead_roi(frame, bbox)
        # OpenCV frames are BGR; index 1 is green.
        green_signal[i] = roi.reshape(-1, roi.shape[-1]).mean(axis=0)[1]

        y1, y2, x1, x2 = chest_roi_bounds(frame.shape, bbox)
        chest_roi = frame[y1:y2, x1:x2]
        gray_chest = cv2.cvtColor(chest_roi, cv2.COLOR_BGR2GRAY) if chest_roi.size > 0 else None

        if prev_gray_chest is not None and gray_chest is not None and gray_chest.shape == prev_gray_chest.shape:
            flow = cv2.calcOpticalFlowFarneback(
                prev_gray_chest, gray_chest, None, 0.5, 3, 15, 3, 5, 1.2, 0
            )
            chest_flow_signal[i] = float(np.mean(flow[..., 1]))
        else:
            chest_flow_signal[i] = 0.0
        prev_gray_chest = gray_chest

    return green_signal, chest_flow_signal


def estimate_vitals_from_signals(
    green_signal: np.ndarray, chest_flow_signal: np.ndarray, fps: float
) -> dict:
    """Filter + FFT both signals into a heart rate and breathing rate estimate."""
    green_detrended = detrend(green_signal)
    hr_filtered = bandpass_filter(green_detrended, fps, *HEART_RATE_BAND_HZ)
    heart_rate = dominant_frequency_bpm(hr_filtered, fps)
    heart_confidence = signal_quality(hr_filtered, fps, HEART_RATE_BAND_HZ)

    chest_detrended = detrend(chest_flow_signal)
    br_filtered = bandpass_filter(chest_detrended, fps, *BREATHING_BAND_HZ)
    breathing_rate = dominant_frequency_bpm(br_filtered, fps)
    breathing_confidence = signal_quality(br_filtered, fps, BREATHING_BAND_HZ)

    return {
        "heart_rate_bpm": round(heart_rate, 1),
        "heart_rate_confidence": round(heart_confidence, 3),
        "breathing_rate_bpm": round(breathing_rate, 1),
        "breathing_rate_confidence": round(breathing_confidence, 3),
    }


# ---------------------------------------------------------------------------
# Face detection + full video pipeline (the real entry point used by the API).
# ---------------------------------------------------------------------------

def _detect_face_bbox(frame: np.ndarray, cascade: cv2.CascadeClassifier):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
    if len(faces) == 0:
        return None
    # Largest detected face wins (closest to camera / most likely the subject).
    faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
    return tuple(int(v) for v in faces[0])


def _smooth_bboxes(bboxes: list):
    """Forward/backward-fill missing detections so a brief dropout doesn't
    break the pipeline; the face doesn't teleport between frames."""
    result = list(bboxes)
    last = None
    for i, b in enumerate(result):
        if b is not None:
            last = b
        elif last is not None:
            result[i] = last
    first_valid = next((b for b in result if b is not None), None)
    for i, b in enumerate(result):
        if b is None:
            result[i] = first_valid
    return result


def read_frames(video_path: str) -> tuple[list[np.ndarray], float]:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise VitalsError(f"Could not open video file: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    return frames, fps


def process_video(video_path: str) -> dict:
    frames, fps = read_frames(video_path)

    min_frames = int(fps * 5)
    if len(frames) < min_frames:
        raise VitalsError(
            f"Clip too short ({len(frames)} frames at {fps:.1f} fps); "
            f"need at least ~5 seconds for a reliable estimate."
        )

    cascade = _get_face_cascade()
    raw_bboxes = [_detect_face_bbox(f, cascade) for f in frames]
    detected_count = sum(1 for b in raw_bboxes if b is not None)
    if detected_count == 0:
        raise VitalsError("No face detected in the clip. Make sure your face is well-lit and centered.")

    bboxes = _smooth_bboxes(raw_bboxes)
    green_signal, chest_flow_signal = extract_signals_from_frames(frames, bboxes)
    result = estimate_vitals_from_signals(green_signal, chest_flow_signal, fps)
    result["fps"] = round(fps, 2)
    result["num_frames"] = len(frames)
    result["face_detection_rate"] = round(detected_count / len(frames), 3)
    return result
