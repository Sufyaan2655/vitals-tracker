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
    is_plausible_bpm,
    signal_quality,
    _resolve_fps,
    _prefer_subharmonic_if_present,
    HEART_RATE_BAND_HZ,
    HEART_RATE_PLAUSIBLE_BPM,
    BREATHING_RATE_PLAUSIBLE_BPM,
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


def test_dominant_frequency_bpm_prefers_fundamental_over_stronger_second_harmonic():
    """Reproduces a real reported bug: the app read 120 bpm against an Apple
    Watch reference of ~55-60 bpm - almost exactly 2x, the classic rPPG
    harmonic-confusion failure. A pulse waveform isn't a pure sine (sharp
    upstroke, slower decay), so it carries real energy at 2x the true rate;
    here the second harmonic is made deliberately *stronger* than the
    fundamental, and the estimate must still land on the fundamental."""
    fs = 30.0
    duration_s = 15
    t = np.arange(0, duration_s, 1.0 / fs)
    true_bpm = 58
    f0 = true_bpm / 60.0

    fundamental = 0.6 * np.sin(2 * np.pi * f0 * t)
    second_harmonic = 1.0 * np.sin(2 * np.pi * 2 * f0 * t)  # stronger than the fundamental
    signal = fundamental + second_harmonic

    filtered = bandpass_filter(signal, fs, *HEART_RATE_BAND_HZ)
    estimated_bpm = dominant_frequency_bpm(filtered, fs, prefer_fundamental=True)
    assert abs(estimated_bpm - true_bpm) <= 3.0, f"expected ~{true_bpm} bpm, got {estimated_bpm:.1f} (locked onto the harmonic)"

    # And confirm the correction is what's actually doing the work here: with
    # it off, this same signal reproduces the original bug (locks onto ~116).
    uncorrected_bpm = dominant_frequency_bpm(filtered, fs, prefer_fundamental=False)
    assert abs(uncorrected_bpm - 2 * true_bpm) <= 3.0


def test_prefer_subharmonic_if_present_switches_when_subharmonic_is_strong():
    freqs = np.array([0.0, 0.5, 1.0, 1.5, 2.0, 2.5])
    magnitude = np.array([0.0, 0.0, 8.0, 0.0, 10.0, 0.0])  # peak at 2.0, comparable energy at 1.0 (half)
    peak_idx = 4  # freqs[4] == 2.0
    assert _prefer_subharmonic_if_present(freqs, magnitude, peak_idx) == 2  # freqs[2] == 1.0


def test_prefer_subharmonic_if_present_keeps_peak_when_no_real_subharmonic():
    freqs = np.array([0.0, 0.5, 1.0, 1.5, 2.0, 2.5])
    magnitude = np.array([0.0, 0.0, 0.2, 0.0, 10.0, 0.0])  # negligible energy at half the peak frequency
    peak_idx = 4
    assert _prefer_subharmonic_if_present(freqs, magnitude, peak_idx) == peak_idx


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
    """dev-mode's plotted spectrum must peak within one raw FFT bin of the
    headline bpm number - otherwise the chart would mislead rather than
    explain the estimate. Not an exact match: dominant_frequency_bpm refines
    the raw bin with sub-bin (parabolic) interpolation, so the two can
    differ by a fraction of a bin's width on purpose."""
    fs = 30.0
    duration_s = 15
    signal = make_sine(1.2, fs, duration_s=duration_s, noise_std=0.1, seed=5)  # 72 bpm
    filtered = bandpass_filter(signal, fs, *HEART_RATE_BAND_HZ)

    freqs, magnitude = fft_spectrum(filtered, fs)
    peak_hz = freqs[int(np.argmax(magnitude))]

    expected_bpm = dominant_frequency_bpm(filtered, fs)
    bin_width_bpm = (fs / (fs * duration_s)) * 60.0  # fs/n in Hz, converted to bpm
    assert abs(peak_hz * 60.0 - expected_bpm) <= bin_width_bpm


def test_dominant_frequency_bpm_interpolates_between_bins():
    """A signal whose true frequency sits deliberately between two raw FFT
    bins should land closer to the true value than either bin alone -
    otherwise every reading is silently quantized to ~4 bpm steps on a
    15-second clip regardless of how clean the underlying signal is."""
    fs = 30.0
    duration_s = 15
    n = int(fs * duration_s)
    bin_width_hz = fs / n
    # A frequency deliberately half a bin off from the nearest FFT bin, and
    # comfortably inside HEART_RATE_BAND_HZ so the band-pass filter doesn't
    # attenuate it away.
    off_bin_hz = 18.5 * bin_width_hz  # ~74 bpm
    true_bpm = off_bin_hz * 60.0

    signal = make_sine(off_bin_hz, fs, duration_s=duration_s, noise_std=0.05, seed=11)
    filtered = bandpass_filter(signal, fs, *HEART_RATE_BAND_HZ)

    estimated_bpm = dominant_frequency_bpm(filtered, fs)
    raw_bin_bpm = round(off_bin_hz / bin_width_hz) * bin_width_hz * 60.0

    assert abs(estimated_bpm - true_bpm) < abs(raw_bin_bpm - true_bpm)


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


def test_is_plausible_bpm_accepts_inside_and_rejects_outside_range():
    assert is_plausible_bpm(70.0, HEART_RATE_PLAUSIBLE_BPM) is True
    assert is_plausible_bpm(45.0, HEART_RATE_PLAUSIBLE_BPM) is True  # inclusive lower bound
    assert is_plausible_bpm(140.0, HEART_RATE_PLAUSIBLE_BPM) is True  # inclusive upper bound
    assert is_plausible_bpm(199.0, HEART_RATE_PLAUSIBLE_BPM) is False
    assert is_plausible_bpm(30.0, HEART_RATE_PLAUSIBLE_BPM) is False


def test_estimate_vitals_from_signals_flags_implausible_heart_rate():
    """A clean, confident sine wave at a physiologically implausible frequency
    (e.g. picked up noise near the edge of the detection band) should still
    be flagged as implausible even though signal_quality reports it as clean -
    that's the whole point of a check independent of confidence."""
    fs = 30.0
    n = int(fs * 15)
    t = np.arange(n) / fs

    implausible_hz = 210 / 60.0  # inside HEART_RATE_BAND_HZ (up to 240bpm), outside plausible range
    green_signal = 128 + 15 * np.sin(2 * np.pi * implausible_hz * t)
    chest_flow_signal = 0.5 * np.sin(2 * np.pi * (16 / 60.0) * t)

    result = estimate_vitals_from_signals(green_signal, chest_flow_signal, fs)

    assert result["heart_rate_bpm"] > HEART_RATE_PLAUSIBLE_BPM[1]
    assert result["heart_rate_plausible"] is False
    assert result["heart_rate_confidence"] > 0.2  # confident AND implausible - the exact case this catches
    assert result["breathing_rate_plausible"] is True


def test_resolve_fps_trusts_a_plausible_reported_value():
    # 447 frames over what the container claims is a real ~30fps clip.
    assert _resolve_fps(reported_fps=29.97, frame_count=447, duration_ms=14915) == 29.97


def test_resolve_fps_falls_back_to_measured_when_reported_is_implausible():
    """Reproduces the real bug: a WebM recording where OpenCV reports a
    nonsense container fps (1000, turning a real ~15s clip into a reported
    0.447s and wrongly tripping the too-short-clip check). 447 frames
    actually decoded over ~14.9 real seconds is obviously a ~30fps clip."""
    fps = _resolve_fps(reported_fps=1000.0, frame_count=447, duration_ms=14915)
    assert 29.0 <= fps <= 31.0


def test_resolve_fps_falls_back_to_default_when_nothing_is_usable():
    assert _resolve_fps(reported_fps=0.0, frame_count=0, duration_ms=0.0) == 30.0
    assert _resolve_fps(reported_fps=None, frame_count=1, duration_ms=0.0) == 30.0


def test_resolve_fps_rejects_an_implausible_measured_fallback_too():
    # duration_ms suspiciously tiny relative to frame_count would itself
    # measure an implausible fps (e.g. a corrupt/truncated read) - don't
    # trust that either, fall back to the default instead.
    fps = _resolve_fps(reported_fps=1000.0, frame_count=447, duration_ms=10)
    assert fps == 30.0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
