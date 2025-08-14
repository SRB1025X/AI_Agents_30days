// ── Session management ─────────────────────────────────────────────────────────
let sessionId = (() => {
  const p = new URLSearchParams(window.location.search);
  let id = p.get("session_id");
  if (!id) { id = Date.now().toString(); p.set("session_id", id); history.replaceState(null, "", "?" + p.toString()); }
  return id;
})();
document.getElementById("sessionIdText").textContent = sessionId;

// ── UI helpers ────────────────────────────────────────────────────────────────
const recordBtn  = document.getElementById("recordBtn");
const statusText = document.getElementById("statusText");
const toast      = document.getElementById("toast");
const aiAudio    = document.getElementById("aiAudio");
const chatWindow = document.getElementById("chatWindow");

function setStatus(msg) {
  statusText.textContent = msg;
}
function showToast(msg) {
  toast.textContent = msg;
  toast.classList.remove("hidden");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toast.classList.add("hidden"), 3000);
}
function appendMsg(role, text) {
  const isUser = role === "user";
  const row = document.createElement("div");
  row.className = `flex ${isUser ? "justify-end" : "justify-start"} w-full`;
  const bubble = document.createElement("div");
  bubble.className = `max-w-[85%] rounded-2xl px-4 py-3 shadow
    ${isUser ? "bg-indigo-500 text-white rounded-br-md" : "bg-white/90 text-gray-900 rounded-bl-md"}`;
  bubble.textContent = text;
  row.appendChild(bubble);
  chatWindow.appendChild(row);
  chatWindow.scrollTop = chatWindow.scrollHeight;
}

// ── Recording state ───────────────────────────────────────────────────────────
let mediaRecorder, audioChunks = [], recordedBlob = null, isRecording = false;

async function startRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });
    audioChunks = [];

    mediaRecorder.ondataavailable = e => { if (e.data.size) audioChunks.push(e.data); };
    mediaRecorder.onstart = () => {
      isRecording = true;
      recordBtn.classList.add("pulse","ring-4","ring-indigo-300","bg-indigo-50");
      recordBtn.setAttribute("aria-pressed","true");
      setStatus("Listening… tap to stop");
    };
    mediaRecorder.onstop = () => {
      isRecording = false;
      recordBtn.classList.remove("pulse","ring-4","ring-indigo-300","bg-indigo-50");
      recordBtn.setAttribute("aria-pressed","false");
      setStatus("Processing…");

      recordedBlob = new Blob(audioChunks, { type: "audio/webm" });
      // optimistic user bubble
      appendMsg("user", "🎙️ (captured voice)");
      // send to agent
      sendToAgent(recordedBlob).catch(err => {
        console.error(err);
        setStatus("Error — tap to try again");
        showToast("⚠️ " + err.message);
      });
    };

    mediaRecorder.start();
  } catch (err) {
    console.error(err);
    showToast("🎙️ Microphone permission required");
    setStatus("Mic blocked — allow and retry");
  }
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== "inactive") mediaRecorder.stop();
}

recordBtn.addEventListener("click", () => {
  if (isRecording) stopRecording(); else startRecording();
});

// ── Networking helper ─────────────────────────────────────────────────────────
async function fetchJson(url, options = {}) {
  const res = await fetch(url, options);
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    const msg = (data && (data.error || data.stage || data.detail)) || `${res.status} ${res.statusText}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data || {};
}

// ── Agent call (voice -> STT -> LLM -> TTS) ───────────────────────────────────
async function sendToAgent(blob) {
  const fd = new FormData();
  fd.append("file", blob, `turn_${Date.now()}.webm`);

  setStatus("Thinking…");
  const data = await fetchJson(`/agent/chat/${sessionId}`, { method: "POST", body: fd });

  // Replace the placeholder by appending real transcript
  if (data.transcript) appendMsg("user", data.transcript);
  appendMsg("assistant", data.llm_text || "(no response)");

  // Autoplay AI voice (hidden player)
  aiAudio.src = data.audio_url || "/static/fallback.mp3";
  try { await aiAudio.play(); } catch {}
  setStatus(data.fallback ? "Speaking (fallback)..." : "Speaking…");

  // Auto re-listen when AI finishes
  aiAudio.onended = () => {
    setStatus("Tap to start recording");
    // Auto-start listening again for fluid back-and-forth
    startRecording();
  };
}
