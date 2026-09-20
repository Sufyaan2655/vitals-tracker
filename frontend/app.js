const API_BASE = ""; // same-origin: FastAPI serves this file itself

const usernameInput = document.getElementById("username");
const preview = document.getElementById("preview");
const startCameraBtn = document.getElementById("startCamera");
const recordBtn = document.getElementById("recordBtn");
const countdownEl = document.getElementById("countdown");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const hrValue = document.getElementById("hrValue");
const hrConfidence = document.getElementById("hrConfidence");
const brValue = document.getElementById("brValue");
const brConfidence = document.getElementById("brConfidence");
const noHistoryEl = document.getElementById("noHistory");

const RECORD_SECONDS = 15;

let mediaStream = null;
let mediaRecorder = null;
let recordedChunks = [];
let chart = null;

// Remember the last-used name across visits (this is a normal local web app
// running in the user's own browser, not an embedded preview, so localStorage
// behaves normally here).
try {
  const savedName = localStorage.getItem("vitals_username");
  if (savedName) usernameInput.value = savedName;
} catch (e) {
  /* private browsing / storage disabled - not fatal, just skip persistence */
}

usernameInput.addEventListener("change", () => {
  try {
    localStorage.setItem("vitals_username", usernameInput.value.trim());
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

  const formData = new FormData();
  formData.append("video", blob, "clip.webm");
  formData.append("username", currentUsername());

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
    setStatus("Done. Recording another clip will add to your trend line below.");
    await refreshHistory();
  } catch (err) {
    setStatus(`Error: ${err.message}`, true);
  } finally {
    recordBtn.disabled = false;
  }
}

function showResult(data) {
  resultsEl.classList.remove("hidden");
  hrValue.textContent = Math.round(data.heart_rate_bpm);
  hrConfidence.textContent = `confidence ${(data.heart_rate_confidence * 100).toFixed(0)}%`;
  brValue.textContent = Math.round(data.breathing_rate_bpm);
  brConfidence.textContent = `confidence ${(data.breathing_rate_confidence * 100).toFixed(0)}%`;
}

async function refreshHistory() {
  try {
    const res = await fetch(`${API_BASE}/api/sessions?username=${encodeURIComponent(currentUsername())}`);
    const sessions = await res.json();
    renderChart(sessions);
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

  const labels = sessions.map((s) =>
    new Date(s.created_at * 1000).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    })
  );
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

// Load any existing history on page load (in case this browser has a saved name).
refreshHistory();
