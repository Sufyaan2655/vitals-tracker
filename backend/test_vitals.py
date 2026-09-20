"""
Verification tests for the rPPG pipeline.

We can't record a real face on a webcam inside this environment, so we validate
the two things that actually matter for correctness *without* needing a real
face video:

1. The core numerical machinery (band-pass filter + FFT peak detection) recovers
   a known frequency from a noisy synthetic signal - this is the actual "does the
   math work" question, independent of face detection quality.
2. The frame -> signal extraction logic correctly reads pixel/optical-flow data
   out of a given bounding box across frames - this is "does the plumbing work",
   using synthetic frames and a hand-supplied bbox so it doesn't depend on
   OpenCV's Haar cascade finding a face in a fake image.

Haar-cascade face *detection* accuracy itself is a separate, well-established
concern (OpenCV's own test suite covers that) and isn't re-validated here.
"""

import numpy as np
import pytest

from vitals import (
    bandpass_filter,
    dominant_frequency_bpm,
    extract_signals_from_frames,
    estimate_vitals_from_signals,
    fft_spectrum,
    forehead_roi_bounds,
    signal_quality,
    HEART_RATE_BAND_HZ,
)


def make_sine(freq_hz: float, fs: float, duration_s: float, noise_std: float = 0.0, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(0, duration_s, 1.0 / fs)
    signal = np.sin(2 * np.pi * freq_hz * t)
    if noise_std:
        signal = signal + rng.normal(0, noise_std, size=t.shape)
    return signal


@pytest.mark.parametrize("bpm", [48, 72, 90, 130, 200])
def test_dominant_frequency_bpm_recovers_known_heart_rate(bpm):
    fs = 30.0  # typical webcam fps
    freq_hz = bpm / 60.0
    signal = make_sine(freq_hz, fs, duration_s=15, noise_std=0.3, seed=bpm)
    filtered = bandpass_filter(signal, fs, *HEART_RATE_BAND_HZ)
    estimated_bpm = dominant_frequency_bpm(filtered, fs)
    assert abs(estimated_bpm - bpm) <= 2.0, f"expected ~{bpm} bpm, got {estimated_bpm:.1f}"


@pytest.mark.parametrize("breaths_per_min", [8, 14, 20, 28])
def test_dominant_frequency_bpm_recovers_known_breathing_rate(breaths_per_min):
    fs = 30.0
    freq_hz = breaths_per_min / 60.0
    signal = make_sine(freq_hz, fs, duration_s=20, noise_std=0.2, seed=breaths_per_min)
    filtered = bandpass_filter(signal, fs, 0.1, 0.6)
    estimated = dominant_frequency_bpm(filtered, fs)
    assert abs(estimated - breaths_per_min) <= 1.5


def test_bandpass_filter_rejects_out_of_band_frequency():
    fs = 30.0
    # A 0.05 Hz drift (way below the heart-rate band) mixed with a real 1.2 Hz
    # (72 bpm) pulse - the filter should suppress the drift and keep the pulse.
    t = np.arange(0, 20, 1.0 / fs)
    drift = 5.0 * np.sin(2 * np.pi * 0.05 * t)
    pulse = 0.2 * np.sin(2 * np.pi * 1.2 * t)
    filtered = bandpass_filter(drift + pulse, fs, *HEART_RATE_BAND_HZ)
    estimated_bpm = dominant_frequency_bpm(filtered, fs)
    assert abs(estimated_bpm - 72) <= 2.0


def test_signal_quality_is_higher_for_clean_periodic_signal_than_noise():
    fs = 30.0
    clean = make_sine(1.2, fs, duration_s=15, noise_std=0.05, seed=1)
    noisy = np.random.default_rng(2).normal(0, 1, size=clean.shape)
    clean_score = signal_quality(bandpass_filter(clean, fs, *HEART_RATE_BAND_HZ), fs, HEART_RATE_BAND_HZ)
    noisy_score = signal_quality(bandpass_filter(noisy, fs, *HEART_RATE_BAND_HZ), fs, HEART_RATE_BAND_HZ)
    assert clean_score > noisy_score


def _make_synthetic_frame(height, width, green_value, brightness):
    frame = np.full((height, width, 3), brightness, dtype=np.uint8)
    # Round (not truncate) so the uint8 frame stores the nearest representable
    # value to green_value - keeps the round-trip precise to within 0.5.
    frame[:, :, 1] = np.clip(np.round(green_value), 0, 255).astype(np.uint8)
    return frame


def test_extract_signals_from_frames_reads_correct_roi_green_channel():
    """The forehead ROI is a known sub-rectangle of the bbox; if we paint that
    exact region a known green value per frame, the extracted signal should
    match it, proving the ROI math (not just the filter math) is correct."""
    fs = 30.0
    duration_s = 10
    n_frames = int(fs * duration_s)
    height, width = 200, 200
    bbox = (50, 50, 100, 100)  # x, y, w, h

    freq_hz = 72 / 60.0
    t = np.arange(n_frames) / fs
    green_values = 128 + 20 * np.sin(2 * np.pi * freq_hz * t)

    frames = [
        _make_synthetic_frame(height, width, green_value=green_values[i], brightness=100)
        for i in range(n_frames)
    ]
    bboxes = [bbox] * n_frames

    green_signal, chest_flow_signal = extract_signals_from_frames(frames, bboxes)

    # Every frame is uniformly colored inside the ROI, so the extracted mean
    # should equal the value we painted, frame for frame (within uint8
    # rounding error, since real video frames are 8-bit per channel too).
    np.testing.assert_allclose(green_signal, np.round(green_values), atol=0.51)
    assert chest_flow_signal.shape == (n_frames,)


def test_estimate_vitals_from_signals_end_to_end_on_synthetic_data():
    fs = 30.0
    duration_s = 15
    n = int(fs * duration_s)
    t = np.arange(n) / fs

    hr_hz = 75 / 60.0
    br_hz = 16 / 60.0

    rng = np.random.default_rng(42)
    green_signal = 128 + 15 * np.sin(2 * np.pi * hr_hz * t) + rng.normal(0, 1.0, n)
    chest_flow_signal = 0.5 * np.sin(2 * np.pi * br_hz * t) + rng.normal(0, 0.05, n)

    result = estimate_vitals_from_signals(green_signal, chest_flow_signal, fs)

    assert abs(result["heart_rate_bpm"] - 75) <= 2.5
    assert abs(result["breathing_rate_bpm"] - 16) <= 1.5
    assert result["heart_rate_confidence"] > 0.2
    assert result["breathing_rate_confidence"] > 0.2


def test_fft_spectrum_peak_matches_dominant_frequency_bpm():
    """dev-mode's plotted spectrum must peak at the same frequency the
    headline bpm number is derived from - otherwise the chart would mislead
    rather than explain the estimate."""
    fs = 30.0
    signal = make_sine(1.2, fs, duration_s=15, noise_std=0.1, seed=5)  # 72 bpm
    filtered = bandpass_filter(signal, fs, *HEART_RATE_BAND_HZ)

    freqs, magnitude = fft_spectrum(filtered, fs)
    peak_hz = freqs[int(np.argmax(magnitude))]

    expected_bpm = dominant_frequency_bpm(filtered, fs)
    assert abs(peak_hz * 60.0 - expected_bpm) < 1e-6


def test_forehead_roi_bounds_is_inside_face_bbox():
    bbox = (50, 50, 100, 100)  # x, y, w, h
    x1, y1, x2, y2 = forehead_roi_bounds(bbox)
    assert bbox[0] <= x1 < x2 <= bbox[0] + bbox[2]
    assert bbox[1] <= y1 < y2 <= bbox[1] + bbox[3]


def test_estimate_vitals_from_signals_debug_arrays_match_input_length():
    fs = 30.0
    n = int(fs * 15)
    t = np.arange(n) / fs
    green_signal = 128 + 15 * np.sin(2 * np.pi * (75 / 60.0) * t)
    chest_flow_signal = 0.5 * np.sin(2 * np.pi * (16 / 60.0) * t)

    result = estimate_vitals_from_signals(green_signal, chest_flow_signal, fs, include_debug=True)

    debug = result["debug"]
    assert len(debug["green_signal_raw"]) == n
    assert len(debug["green_signal_filtered"]) == n
    assert len(debug["chest_signal_raw"]) == n
    assert len(debug["chest_signal_filtered"]) == n
    assert len(debug["hr_fft_freqs_hz"]) == len(debug["hr_fft_magnitude"])
    assert len(debug["br_fft_freqs_hz"]) == len(debug["br_fft_magnitude"])

    # include_debug=False (the default) must not pay for or expose any of this.
    plain = estimate_vitals_from_signals(green_signal, chest_flow_signal, fs)
    assert "debug" not in plain


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
