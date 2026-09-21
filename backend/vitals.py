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

# Narrower than the detection bands above on purpose: those exist so the FFT
# can find a peak at all, but a peak landing near their edges (e.g. 220 bpm)
# is almost never a real resting reading - it's confidently measuring noise.
# These flag "atypical for someone sitting still," not "wrong"; confidence
# alone can't catch this because a clean noise peak still scores high.
HEART_RATE_PLAUSIBLE_BPM = (45.0, 140.0)
BREATHING_RATE_PLAUSIBLE_BPM = (8.0, 28.0)

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


SUBHARMONIC_MAGNITUDE_RATIO = 0.5


def dominant_frequency_bpm(signal: np.ndarray, fs: float, prefer_fundamental: bool = False) -> float:
    """Return the dominant frequency of `signal`, expressed in cycles/minute (bpm).

    `prefer_fundamental=True` applies a subharmonic correction: a real pulse
    waveform isn't a clean sine wave - a sharp systolic upstroke followed by
    a slower diastolic decay - so it routinely carries strong energy at 2x
    the true pulse rate. Picking the single tallest FFT bin can then lock
    onto that second harmonic and report exactly double the real heart rate
    (this is the root cause of a 120bpm reading against a ~58bpm reference:
    120/58 is almost exactly 2). If there's a comparably strong peak at half
    the frequency of the tallest bin, that's almost certainly the true
    fundamental, so it's preferred instead. Scoped to heart rate on purpose:
    breathing motion is close enough to sinusoidal that this correction does
    more harm than good there (it was tried and produced false positives
    near the breathing band's low edge), so estimate_vitals_from_signals
    only passes this for the heart-rate call.

    The returned frequency is refined with quadratic interpolation around
    the chosen bin (see `_parabolic_interpolate`) rather than snapped to the
    raw bin - a 15-second clip only gives ~4 bpm of raw FFT resolution, so
    without this every reading is silently quantized to the nearest ~4 bpm
    regardless of how clean the signal actually is. This applies to both
    heart rate and breathing rate equally, since both go through this same
    function.
    """
    signal = np.asarray(signal, dtype=np.float64)
    n = len(signal)
    if n < 4:
        raise VitalsError("Signal too short to estimate a frequency")
    windowed = signal * np.hanning(n)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    magnitude = np.abs(np.fft.rfft(windowed))
    magnitude[0] = 0.0  # ignore DC component
    peak_idx = int(np.argmax(magnitude))
    if prefer_fundamental:
        peak_idx = _prefer_subharmonic_if_present(freqs, magnitude, peak_idx)
    refined_idx = _parabolic_interpolate(magnitude, peak_idx)
    freq_step = freqs[1] - freqs[0] if len(freqs) > 1 else 0.0
    refined_hz = freqs[peak_idx] + (refined_idx - peak_idx) * freq_step
    return float(refined_hz * 60.0)


def _parabolic_interpolate(magnitude: np.ndarray, peak_idx: int) -> float:
    """Refine a discrete FFT peak to a fractional bin position by fitting a
    parabola through the peak and its two neighboring bins - a standard,
    well-established technique (used the same way in audio pitch detection)
    for locating a spectral peak more precisely than the raw bin spacing
    allows, without needing a longer signal or a bigger FFT. Falls back to
    the untouched integer bin at the spectrum's edges, where there's no
    second neighbor to fit against.
    """
    if peak_idx <= 0 or peak_idx >= len(magnitude) - 1:
        return float(peak_idx)
    y1, y2, y3 = magnitude[peak_idx - 1], magnitude[peak_idx], magnitude[peak_idx + 1]
    denom = y1 - 2 * y2 + y3
    if denom == 0:
        return float(peak_idx)
    offset = 0.5 * (y1 - y3) / denom
    offset = float(np.clip(offset, -0.5, 0.5))
    return peak_idx + offset


def _prefer_subharmonic_if_present(freqs: np.ndarray, magnitude: np.ndarray, peak_idx: int) -> int:
    half_freq = freqs[peak_idx] / 2.0
    if half_freq <= 0:
        return peak_idx
    half_idx = int(np.argmin(np.abs(freqs - half_freq)))
    if half_idx == peak_idx or half_idx == 0:
        return peak_idx
    if magnitude[half_idx] >= SUBHARMONIC_MAGNITUDE_RATIO * magnitude[peak_idx]:
        return half_idx
    return peak_idx


def is_plausible_bpm(value: float, plausible_range: tuple[float, float]) -> bool:
    """Whether `value` falls inside a resting-plausible sub-range - a check
    independent of signal_quality/confidence, since confidence measures how
    concentrated the spectral energy is, not whether the resulting number
    makes physiological sense for someone sitting still."""
    low, high = plausible_range
    return low <= value <= high


def fft_spectrum(signal: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (freqs_hz, magnitude) of `signal`'s FFT - the same spectrum
    dominant_frequency_bpm picks its peak from, exposed so a caller (dev-mode
    debug output) can plot it."""
    signal = np.asarray(signal, dtype=np.float64)
    n = len(signal)
    windowed = signal * np.hanning(n)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    magnitude = np.abs(np.fft.rfft(windowed))
    magnitude[0] = 0.0  # ignore DC component, matches dominant_frequency_bpm
    return freqs, magnitude


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

def forehead_roi_bounds(bbox: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """The forehead sub-rectangle of a face bbox, as (x1, y1, x2, y2) - the
    single source of truth for where forehead_roi crops from, also used to
    report the ROI's location for dev-mode overlays."""
    x, y, w, h = bbox
    x1, x2 = x + int(w * 0.30), x + int(w * 0.70)
    y1, y2 = y + int(h * 0.05), y + int(h * 0.25)
    return x1, y1, x2, y2


def forehead_roi(frame: np.ndarray, bbox: tuple[int, int, int, int]) -> np.ndarray:
    """Crop the forehead sub-region of a face bounding box (most motion-stable,
    least occluded-by-glasses/beard part of the face for rPPG)."""
    x1, y1, x2, y2 = forehead_roi_bounds(bbox)
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        x, y, w, h = bbox
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
    green_signal: np.ndarray,
    chest_flow_signal: np.ndarray,
    fps: float,
    include_debug: bool = False,
) -> dict:
    """Filter + FFT both signals into a heart rate and breathing rate estimate.

    With `include_debug=True`, also returns the intermediate raw/filtered
    signals and their FFT spectra under a "debug" key, so a caller (dev-mode
    UI) can show *why* a given bpm/confidence was picked instead of just the
    final numbers - recomputing this from the same filtered arrays the
    estimate itself used, rather than duplicating the filtering logic.
    """
    green_detrended = detrend(green_signal)
    hr_filtered = bandpass_filter(green_detrended, fps, *HEART_RATE_BAND_HZ)
    heart_rate = dominant_frequency_bpm(hr_filtered, fps, prefer_fundamental=True)
    heart_confidence = signal_quality(hr_filtered, fps, HEART_RATE_BAND_HZ)

    chest_detrended = detrend(chest_flow_signal)
    br_filtered = bandpass_filter(chest_detrended, fps, *BREATHING_BAND_HZ)
    breathing_rate = dominant_frequency_bpm(br_filtered, fps)
    breathing_confidence = signal_quality(br_filtered, fps, BREATHING_BAND_HZ)

    result = {
        "heart_rate_bpm": round(heart_rate, 1),
        "heart_rate_confidence": round(heart_confidence, 3),
        "heart_rate_plausible": is_plausible_bpm(heart_rate, HEART_RATE_PLAUSIBLE_BPM),
        "breathing_rate_bpm": round(breathing_rate, 1),
        "breathing_rate_confidence": round(breathing_confidence, 3),
        "breathing_rate_plausible": is_plausible_bpm(breathing_rate, BREATHING_RATE_PLAUSIBLE_BPM),
    }

    if include_debug:
        hr_freqs, hr_mag = fft_spectrum(hr_filtered, fps)
        br_freqs, br_mag = fft_spectrum(br_filtered, fps)
        result["debug"] = {
            "green_signal_raw": np.asarray(green_signal, dtype=np.float64).tolist(),
            "green_signal_filtered": hr_filtered.tolist(),
            "chest_signal_raw": np.asarray(chest_flow_signal, dtype=np.float64).tolist(),
            "chest_signal_filtered": br_filtered.tolist(),
            "hr_fft_freqs_hz": hr_freqs.tolist(),
            "hr_fft_magnitude": hr_mag.tolist(),
            "br_fft_freqs_hz": br_freqs.tolist(),
            "br_fft_magnitude": br_mag.tolist(),
            "heart_rate_band_hz": list(HEART_RATE_BAND_HZ),
            "breathing_band_hz": list(BREATHING_BAND_HZ),
        }

    return result


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


PLAUSIBLE_CAMERA_FPS = (5.0, 120.0)


def _resolve_fps(reported_fps: float, frame_count: int, duration_ms: float) -> float:
    """Pick a usable fps for a decoded clip.

    Some WebM streams (e.g. from the browser's MediaRecorder, which is what
    every real recording in this app is) don't embed a reliable container
    frame rate, and OpenCV/ffmpeg can report nonsense for them - observed in
    practice: 1000.0 fps for a real ~30fps, ~15s clip, which silently turns
    "447 frames" into "0.447 seconds" and trips the too-short-clip check on
    a perfectly good recording. Synthetic MP4 test clips (cv2.VideoWriter)
    don't hit this, which is why it went unnoticed until a real browser
    recording. Trust the container's reported fps only if it's in a
    plausible camera range; otherwise derive it from how long the frames
    OpenCV actually decoded really spanned; otherwise fall back to 30.
    """
    low, high = PLAUSIBLE_CAMERA_FPS
    if reported_fps and low <= reported_fps <= high:
        return reported_fps
    if duration_ms and duration_ms > 0 and frame_count > 1:
        measured_fps = frame_count / (duration_ms / 1000.0)
        if low <= measured_fps <= high:
            return measured_fps
    return 30.0


def read_frames(video_path: str) -> tuple[list[np.ndarray], float]:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise VitalsError(f"Could not open video file: {video_path}")
    reported_fps = cap.get(cv2.CAP_PROP_FPS)
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    duration_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
    cap.release()
    fps = _resolve_fps(reported_fps, len(frames), duration_ms)
    return frames, fps


def detect_face_regions_from_bytes(image_bytes: bytes) -> dict | None:
    """Decode a single JPEG/PNG frame and detect the same face/forehead/chest
    regions process_video() uses - the one source of truth for the live
    camera-preview overlay, so what's drawn while you're recording matches
    exactly what the real pipeline looks at. Returns None if no face found."""
    npimg = np.frombuffer(image_bytes, dtype=np.uint8)
    frame = cv2.imdecode(npimg, cv2.IMREAD_COLOR)
    if frame is None:
        raise VitalsError("Could not decode frame")

    cascade = _get_face_cascade()
    bbox = _detect_face_bbox(frame, cascade)
    if bbox is None:
        return None

    height, width = frame.shape[:2]
    fx1, fy1, fx2, fy2 = forehead_roi_bounds(bbox)
    cy1, cy2, cx1, cx2 = chest_roi_bounds((height, width), bbox)
    return {
        "face_bbox": list(bbox),
        "forehead_roi_bbox": [fx1, fy1, fx2 - fx1, fy2 - fy1],
        "chest_roi_bbox": [cx1, cy1, cx2 - cx1, cy2 - cy1],
        "frame_width": width,
        "frame_height": height,
    }


def process_video(video_path: str, include_debug: bool = False) -> dict:
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
    result = estimate_vitals_from_signals(green_signal, chest_flow_signal, fps, include_debug=include_debug)
    result["fps"] = round(fps, 2)
    result["num_frames"] = len(frames)
    result["face_detection_rate"] = round(detected_count / len(frames), 3)

    if include_debug:
        height, width = frames[0].shape[:2]
        forehead_boxes = []
        chest_boxes = []
        for b in bboxes:
            fx1, fy1, fx2, fy2 = forehead_roi_bounds(b)
            forehead_boxes.append([fx1, fy1, fx2 - fx1, fy2 - fy1])
            cy1, cy2, cx1, cx2 = chest_roi_bounds((height, width), b)
            chest_boxes.append([cx1, cy1, cx2 - cx1, cy2 - cy1])

        result["debug"]["frame_width"] = width
        result["debug"]["frame_height"] = height
        result["debug"]["frame_times_s"] = [round(i / fps, 4) for i in range(len(frames))]
        result["debug"]["face_detected"] = [b is not None for b in raw_bboxes]
        result["debug"]["face_bboxes"] = [list(b) for b in bboxes]
        result["debug"]["forehead_roi_bboxes"] = forehead_boxes
        result["debug"]["chest_roi_bboxes"] = chest_boxes

    return result
