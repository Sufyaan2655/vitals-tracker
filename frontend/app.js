const API_BASE = ""; // same-origin: FastAPI serves this file itself

const usernameInput = document.getElementById("username");
const preview = document.getElementById("preview");
const startCameraBtn = document.getElementById("startCamera");
const recordBtn = document.getElementById("recordBtn");
const devModeToggle = document.getElementById("devMode");
const countdownEl = document.getElementById("countdown");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const hrValue = document.getElementById("hrValue");
const hrConfidence = document.getElementById("hrConfidence");
const brValue = document.getElementById("brValue");
const brConfidence = document.getElementById("brConfidence");
const noHistoryEl = document.getElementById("noHistory");
const historyTableWrap = document.getElementById("historyTableWrap");
const historyTableBody = document.getElementById("historyTableBody");
const historyStatsEl = document.getElementById("historyStats");
const exportCsvBtn = document.getElementById("exportCsv");

const devInsightsEl = document.getElementById("devInsights");
const replayVideo = document.getElementById("replayVideo");
const overlayCanvas = document.getElementById("overlayCanvas");
const faceDropoutWarning = document.getElementById("faceDropoutWarning");

const RECORD_SECONDS = 15;
// Must match backend/vitals.py's HEART_RATE_BAND_HZ / BREATHING_BAND_HZ - the
// physiologically plausible ranges the FFT peak is picked from.
const HEART_RATE_BAND_BPM = [0.7 * 60, 4.0 * 60];
const BREATHING_BAND_BPM = [0.1 * 60, 0.6 * 60];

let mediaStream = null;
let mediaRecorder = null;
let recordedChunks = [];
let chart = null;
let confidenceChart = null;

let debugData = null;
let debugFps = 30;
let replayObjectUrl = null;
let overlayLoopActive = false;
const devCharts = {};

// Remember the last-used name/dev-mode preference across visits (this is a
// normal local web app running in the user's own browser, not an embedded
// preview, so localStorage behaves normally here).
try {
  const savedName = localStorage.getItem("vitals_username");
  if (savedName) usernameInput.value = savedName;
  devModeToggle.checked = localStorage.getItem("vitals_dev_mode") === "1";
} catch (e) {
  /* private browsing / storage disabled - not fatal, just skip persistence */
}

usernameInput.addEventListener("change", () => {
  try {
    localStorage.setItem("vitals_username", usernameInput.value.trim());
  } catch (e) {}
  refreshHistory();
});

devModeToggle.addEventListener("change", () => {
  try {
    localStorage.setItem("vitals_dev_mode", devModeToggle.checked ? "1" : "0");
  } catch (e) {}
});

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.classList.toggle("error", isError);
}

function currentUsername() {
  return usernameInput.value.trim() || "anonymous";
}

startCameraBtn.addEventListener("click", async () => {
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      video: { width: 640, height: 480, facingMode: "user" },
      audio: false,
    });
    preview.srcObject = mediaStream;
    recordBtn.disabled = false;
    startCameraBtn.disabled = true;
    setStatus("Camera ready. Sit still, face well-lit, then record a clip.");
  } catch (err) {
    setStatus(
      "Couldn't access the camera. Check browser permissions and that no other app is using it.",
      true
    );
  }
});

recordBtn.addEventListener("click", () => {
  if (!mediaStream) return;
  recordedChunks = [];

  const mimeType = MediaRecorder.isTypeSupported("video/webm;codecs=vp8")
    ? "video/webm;codecs=vp8"
    : "video/webm";
  mediaRecorder = new MediaRecorder(mediaStream, { mimeType });

  mediaRecorder.ondataavailable = (e) => {
    if (e.data.size > 0) recordedChunks.push(e.data);
  };
  mediaRecorder.onstop = handleRecordingComplete;

  mediaRecorder.start();
  recordBtn.disabled = true;
  resultsEl.classList.add("hidden");
  devInsightsEl.classList.add("hidden");
  overlayLoopActive = false;
  setStatus("Recording... stay still.");

  let remaining = RECORD_SECONDS;
  countdownEl.textContent = `${remaining}s remaining`;
  const timer = setInterval(() => {
    remaining -= 1;
    countdownEl.textContent = remaining > 0 ? `${remaining}s remaining` : "";
    if (remaining <= 0) {
      clearInterval(timer);
      mediaRecorder.stop();
    }
  }, 1000);
});

async function handleRecordingComplete() {
  setStatus("Processing clip (detecting face, extracting pulse signal)...");
  const blob = new Blob(recordedChunks, { type: "video/webm" });
  const devMode = devModeToggle.checked;

  const formData = new FormData();
  formData.append("video", blob, "clip.webm");
  formData.append("username", currentUsername());
  formData.append("dev_mode", devMode ? "true" : "false");

  try {
    const res = await fetch(`${API_BASE}/api/sessions/upload`, {
      method: "POST",
      body: formData,
    });
    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.detail || "Processing failed");
    }

    showResult(data);
    if (devMode && data.debug) {
      setupDevInsights(blob, data);
    }
    setStatus("Done. Recording another clip will add to your trend line below.");
    await refreshHistory();
  } catch (err) {
    setStatus(`Error: ${err.message}`, true);
  } finally {
    recordBtn.disabled = false;
  }
}

function confidenceClass(score) {
  if (score >= 0.6) return "conf-high";
  if (score >= 0.35) return "conf-medium";
  return "conf-low";
}

function showResult(data) {
  resultsEl.classList.remove("hidden");
  hrValue.textContent = Math.round(data.heart_rate_bpm);
  hrConfidence.textContent = `confidence ${(data.heart_rate_confidence * 100).toFixed(0)}%`;
  hrConfidence.className = `stat-confidence ${confidenceClass(data.heart_rate_confidence)}`;
  brValue.textContent = Math.round(data.breathing_rate_bpm);
  brConfidence.textContent = `confidence ${(data.breathing_rate_confidence * 100).toFixed(0)}%`;
  brConfidence.className = `stat-confidence ${confidenceClass(data.breathing_rate_confidence)}`;
}

// ---------------------------------------------------------------------------
// Dev mode: replay the just-recorded clip with the face/ROI boxes the backend
// actually used drawn on top, plus the raw + filtered signals and their FFT
// spectra, so it's visible *why* a given bpm/confidence came out the way it did.
// ---------------------------------------------------------------------------

function setupDevInsights(blob, data) {
  devInsightsEl.classList.remove("hidden");
  debugData = data.debug;
  debugFps = data.fps || 30;

  if (replayObjectUrl) URL.revokeObjectURL(replayObjectUrl);
  replayObjectUrl = URL.createObjectURL(blob);
  replayVideo.src = replayObjectUrl;
  replayVideo.currentTime = 0;

  const detectedFrames = debugData.face_detected.filter(Boolean).length;
  const totalFrames = debugData.face_detected.length;
  if (detectedFrames < totalFrames) {
    faceDropoutWarning.textContent =
      `Face detection dropped out on ${totalFrames - detectedFrames} of ${totalFrames} frames ` +
      `(shown in red below) - those frames reuse the last known box rather than skipping, ` +
      `which is a real source of noise in the signal.`;
    faceDropoutWarning.classList.remove("hidden");
  } else {
    faceDropoutWarning.classList.add("hidden");
  }

  replayVideo.onloadedmetadata = () => {
    overlayCanvas.width = debugData.frame_width;
    overlayCanvas.height = debugData.frame_height;
    if (!overlayLoopActive) {
      overlayLoopActive = true;
      requestAnimationFrame(overlayLoop);
    }
  };

  renderDevCharts(data);
}

function overlayLoop() {
  if (devInsightsEl.classList.contains("hidden")) {
    overlayLoopActive = false;
    return;
  }
  drawOverlayFrame();
  requestAnimationFrame(overlayLoop);
}

function drawBox(ctx, bbox, color, label, dashed = false) {
  if (!bbox) return;
  const [x, y, w, h] = bbox;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  if (dashed) ctx.setLineDash([6, 4]);
  ctx.strokeRect(x, y, w, h);
  ctx.restore();
  ctx.fillStyle = color;
  ctx.font = "bold 13px sans-serif";
  const labelY = y > 16 ? y - 5 : y + h + 14;
  ctx.fillText(label, x + 2, labelY);
}

function drawOverlayFrame() {
  if (!debugData) return;
  const idx = Math.min(
    debugData.face_bboxes.length - 1,
    Math.max(0, Math.round(replayVideo.currentTime * debugFps))
  );
  const ctx = overlayCanvas.getContext("2d");
  ctx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);

  const detected = debugData.face_detected[idx];
  drawBox(ctx, debugData.face_bboxes[idx], detected ? "#5b8cff" : "#ff6b6b", detected ? "face" : "face (lost)", !detected);
  drawBox(ctx, debugData.forehead_roi_bboxes[idx], "#33d6a6", "forehead ROI");
  drawBox(ctx, debugData.chest_roi_bboxes[idx], "#ffb84d", "chest ROI");
}

function destroyDevCharts() {
  Object.values(devCharts).forEach((c) => c && c.destroy());
}

function verticalLineDataset(xValue, maxY, color, label, dashed) {
  return {
    label,
    data: [
      { x: xValue, y: 0 },
      { x: xValue, y: maxY },
    ],
    borderColor: color,
    borderWidth: dashed ? 1.5 : 2,
    borderDash: dashed ? [5, 4] : [],
    pointRadius: 0,
    fill: false,
    tension: 0,
  };
}

function makeSignalChart(canvasId, times, raw, filtered, rawLabel, filteredLabel, rawColor, filteredColor) {
  const ctx = document.getElementById(canvasId).getContext("2d");
  const toPoints = (arr) => arr.map((v, i) => ({ x: times[i], y: v }));
  return new Chart(ctx, {
    type: "line",
    data: {
      datasets: [
        {
          label: rawLabel,
          data: toPoints(raw),
          borderColor: rawColor,
          backgroundColor: "transparent",
          borderWidth: 1,
          pointRadius: 0,
          tension: 0.1,
        },
        {
          label: filteredLabel,
          data: toPoints(filtered),
          borderColor: filteredColor,
          backgroundColor: "transparent",
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.1,
        },
      ],
    },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: { type: "linear", title: { display: true, text: "seconds", color: "#93a0b8" }, ticks: { color: "#93a0b8" }, grid: { color: "#262e42" } },
        y: { ticks: { color: "#93a0b8" }, grid: { color: "#262e42" }, title: { display: true, text: "raw (thin) vs. filtered (bold)", color: "#93a0b8", font: { size: 10 } } },
      },
      plugins: { legend: { labels: { color: "#e7ebf3", boxWidth: 12, font: { size: 11 } } } },
    },
  });
}

function makeFftChart(canvasId, freqsHz, magnitude, bandBpm, peakBpm, accentColor, xMax) {
  const bpm = freqsHz.map((f) => f * 60);
  const points = bpm.map((x, i) => ({ x, y: magnitude[i] }));
  const maxMag = Math.max(...magnitude, 1);

  const ctx = document.getElementById(canvasId).getContext("2d");
  return new Chart(ctx, {
    type: "line",
    data: {
      datasets: [
        {
          label: "spectrum",
          data: points,
          borderColor: accentColor,
          backgroundColor: "transparent",
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.15,
        },
        verticalLineDataset(bandBpm[0], maxMag, "#93a0b8", "plausible range", true),
        verticalLineDataset(bandBpm[1], maxMag, "#93a0b8", "plausible range", true),
        verticalLineDataset(peakBpm, maxMag, "#ff6b6b", "detected peak", false),
      ],
    },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: { type: "linear", min: 0, max: xMax, title: { display: true, text: "bpm / min", color: "#93a0b8" }, ticks: { color: "#93a0b8" }, grid: { color: "#262e42" } },
        y: { ticks: { color: "#93a0b8" }, grid: { color: "#262e42" }, title: { display: true, text: "FFT magnitude", color: "#93a0b8", font: { size: 10 } } },
      },
      plugins: {
        legend: {
          labels: {
            color: "#e7ebf3",
            boxWidth: 12,
            font: { size: 11 },
            filter: (item) => item.text !== "plausible range" || item.datasetIndex === 1,
          },
        },
      },
    },
  });
}

function renderDevCharts(data) {
  destroyDevCharts();
  const debug = data.debug;

  devCharts.hrSignal = makeSignalChart(
    "hrSignalChart",
    debug.frame_times_s,
    debug.green_signal_raw,
    debug.green_signal_filtered,
    "raw forehead green channel",
    "band-passed (0.7-4.0 Hz)",
    "#5b8cff",
    "#33d6a6"
  );
  devCharts.brSignal = makeSignalChart(
    "brSignalChart",
    debug.frame_times_s,
    debug.chest_signal_raw,
    debug.chest_signal_filtered,
    "raw chest optical-flow",
    "band-passed (0.1-0.6 Hz)",
    "#5b8cff",
    "#ffb84d"
  );
  devCharts.hrFft = makeFftChart(
    "hrFftChart",
    debug.hr_fft_freqs_hz,
    debug.hr_fft_magnitude,
    HEART_RATE_BAND_BPM,
    data.heart_rate_bpm,
    "#33d6a6",
    260
  );
  devCharts.brFft = makeFftChart(
    "brFftChart",
    debug.br_fft_freqs_hz,
    debug.br_fft_magnitude,
    BREATHING_BAND_BPM,
    data.breathing_rate_bpm,
    "#ffb84d",
    40
  );
}

// ---------------------------------------------------------------------------
// History: trend charts, summary stats, session table (with delete), CSV export.
// ---------------------------------------------------------------------------

async function refreshHistory() {
  try {
    const res = await fetch(`${API_BASE}/api/sessions?username=${encodeURIComponent(currentUsername())}`);
    const sessions = await res.json();
    renderChart(sessions);
    renderConfidenceChart(sessions);
    renderStats(sessions);
    renderHistoryTable(sessions);
  } catch (err) {
    /* history is a nice-to-have; don't block the main flow on it */
  }
}

function renderChart(sessions) {
  if (!sessions || sessions.length === 0) {
    noHistoryEl.style.display = "block";
    if (chart) {
      chart.destroy();
      chart = null;
    }
    return;
  }
  noHistoryEl.style.display = "none";

  const labels = sessions.map((s) => formatSessionTime(s.created_at));
  const hr = sessions.map((s) => s.heart_rate_bpm);
  const br = sessions.map((s) => s.breathing_rate_bpm);

  const ctx = document.getElementById("trendsChart").getContext("2d");
  if (chart) chart.destroy();
  chart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Heart rate (bpm)",
          data: hr,
          borderColor: "#5b8cff",
          backgroundColor: "transparent",
          tension: 0.3,
          yAxisID: "y",
        },
        {
          label: "Breathing rate (br/min)",
          data: br,
          borderColor: "#33d6a6",
          backgroundColor: "transparent",
          tension: 0.3,
          yAxisID: "y1",
        },
      ],
    },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      scales: {
        y: { type: "linear", position: "left", title: { display: true, text: "bpm" } },
        y1: { type: "linear", position: "right", title: { display: true, text: "br/min" }, grid: { drawOnChartArea: false } },
      },
      plugins: {
        legend: { labels: { color: "#e7ebf3" } },
      },
    },
  });
}

function renderConfidenceChart(sessions) {
  const canvas = document.getElementById("confidenceChart");
  if (!sessions || sessions.length === 0) {
    if (confidenceChart) {
      confidenceChart.destroy();
      confidenceChart = null;
    }
    canvas.classList.add("hidden");
    return;
  }
  canvas.classList.remove("hidden");

  const labels = sessions.map((s) => formatSessionTime(s.created_at));
  const hrConf = sessions.map((s) => Math.round(s.heart_rate_confidence * 100));
  const brConf = sessions.map((s) => Math.round(s.breathing_rate_confidence * 100));

  const ctx = canvas.getContext("2d");
  if (confidenceChart) confidenceChart.destroy();
  confidenceChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Heart rate confidence (%)",
          data: hrConf,
          borderColor: "#33d6a6",
          backgroundColor: "transparent",
          tension: 0.3,
        },
        {
          label: "Breathing confidence (%)",
          data: brConf,
          borderColor: "#ffb84d",
          backgroundColor: "transparent",
          tension: 0.3,
        },
      ],
    },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      scales: {
        y: { type: "linear", min: 0, max: 100, title: { display: true, text: "confidence %" } },
      },
      plugins: {
        legend: { labels: { color: "#e7ebf3" } },
      },
    },
  });
}

function renderStats(sessions) {
  if (!sessions || sessions.length === 0) {
    historyStatsEl.innerHTML = "";
    return;
  }
  const avg = (arr) => arr.reduce((a, b) => a + b, 0) / arr.length;
  const hr = sessions.map((s) => s.heart_rate_bpm);
  const br = sessions.map((s) => s.breathing_rate_bpm);

  const cards = [
    [sessions.length, "Sessions"],
    [Math.round(avg(hr)), "Avg HR (bpm)"],
    [Math.round(avg(br)), "Avg BR (br/min)"],
    [Math.min(...hr).toFixed(0) + "-" + Math.max(...hr).toFixed(0), "HR range"],
  ];
  historyStatsEl.innerHTML = cards
    .map(([value, label]) => `<div class="stat-mini"><div class="stat-mini-value">${value}</div><div class="stat-mini-label">${label}</div></div>`)
    .join("");
}

function formatSessionTime(createdAt) {
  return new Date(createdAt * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function renderHistoryTable(sessions) {
  if (!sessions || sessions.length === 0) {
    historyTableWrap.classList.add("hidden");
    historyTableBody.innerHTML = "";
    return;
  }
  historyTableWrap.classList.remove("hidden");

  historyTableBody.innerHTML = sessions
    .slice()
    .reverse()
    .map(
      (s) => `
      <tr>
        <td>${formatSessionTime(s.created_at)}</td>
        <td>${Math.round(s.heart_rate_bpm)}</td>
        <td>${Math.round(s.breathing_rate_bpm)}</td>
        <td>${(s.heart_rate_confidence * 100).toFixed(0)}% / ${(s.breathing_rate_confidence * 100).toFixed(0)}%</td>
        <td><button class="row-delete" data-id="${s.id}" title="Delete this session">&times;</button></td>
      </tr>`
    )
    .join("");
}

historyTableBody.addEventListener("click", async (e) => {
  const btn = e.target.closest(".row-delete");
  if (!btn) return;
  const id = btn.dataset.id;
  btn.disabled = true;
  try {
    const res = await fetch(`${API_BASE}/api/sessions/${id}?username=${encodeURIComponent(currentUsername())}`, {
      method: "DELETE",
    });
    if (!res.ok) throw new Error("Delete failed");
    await refreshHistory();
  } catch (err) {
    setStatus(`Couldn't delete session: ${err.message}`, true);
    btn.disabled = false;
  }
});

exportCsvBtn.addEventListener("click", async () => {
  try {
    const res = await fetch(`${API_BASE}/api/sessions?username=${encodeURIComponent(currentUsername())}`);
    const sessions = await res.json();
    if (!sessions.length) return;

    const columns = [
      "id",
      "created_at",
      "heart_rate_bpm",
      "heart_rate_confidence",
      "breathing_rate_bpm",
      "breathing_rate_confidence",
      "fps",
      "num_frames",
      "face_detection_rate",
    ];
    const rows = sessions.map((s) => columns.map((c) => s[c]).join(","));
    const csv = [columns.join(","), ...rows].join("\n");

    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `vitals_${currentUsername()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    setStatus(`Couldn't export CSV: ${err.message}`, true);
  }
});

// Load any existing history on page load (in case this browser has a saved name).
refreshHistory();
