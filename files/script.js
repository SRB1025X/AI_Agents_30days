// ========== Session handling ==========
function ensureSessionId() {
  const params = new URLSearchParams(window.location.search);
  let id = params.get("session_id");
  if (!id) {
    id = Date.now().toString();
    params.set("session_id", id);
    history.replaceState(null, "", "?" + params.toString());
  }
  const el = document.getElementById("sessionIdText");
  if (el) el.textContent = id;
  return id;
}
const sessionId = ensureSessionId();

// ========== UI helpers ==========
const statusText = document.getElementById("statusText");
const recordBtn  = document.getElementById("recordBtn");
const micIcon    = document.getElementById("micIcon");
const chatWindow = document.getElementById("chatWindow");
const aiAudio    = document.getElementById("aiAudio");
const toast      = document.getElementById("toast");

function showToast(msg) {
  toast.textContent = msg;
  toast.classList.remove("hidden");
  setTimeout(() => toast.classList.add("hidden"), 2000);
}

function addMessage(text, side = "right") {
  const wrap = document.createElement("div");
  wrap.className = `w-full flex ${side === "right" ? "justify-end" : "justify-start"}`;
  const bubble = document.createElement("div");
  bubble.className =
    "msg px-4 py-2 rounded-2xl shadow text-sm " +
    (side === "right"
      ? "bg-indigo-500/90 text-white"
      : "bg-white text-gray-900");
  bubble.textContent = text;
  wrap.appendChild(bubble);
  chatWindow.appendChild(wrap);
  chatWindow.scrollTop = chatWindow.scrollHeight;
}

// ========== Recording + send to server (/agent/chat/{session_id}) ==========
let mediaRecorder;
let chunks = [];
let isRecording = false;

async function startRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream, { mimeType: "audio/webm;codecs=opus" });
    chunks = [];

    mediaRecorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) chunks.push(e.data);
    };

    mediaRecorder.onstop = async () => {
      try {
        const blob = new Blob(chunks, { type: "audio/webm" });
        addMessage("(captured voice)", "right");

        // Send voice → /agent/chat/{session_id}
        const fd = new FormData();
        fd.append("file", blob, `rec_${Date.now()}.webm`);
        // NEW: pass toggle state (default off)
        fd.append("concise", document.getElementById("conciseToggle")?.checked ? "1" : "0");
        //const webToggle = document.getElementById("webToggle");
        fd.append("web_search", document.getElementById("webToggle")?.checked ? "1" : "0");
        // NEW: include user-provided API keys (if present in localStorage)
        const k = {
            assemblyai: localStorage.getItem("key_assemblyai") || "",
            gemini:     localStorage.getItem("key_gemini")     || "",
            murf:       localStorage.getItem("key_murf")       || "",
            tavily:     localStorage.getItem("key_tavily")     || "",
        };
        if (k.assemblyai) fd.append("assemblyai_api_key", k.assemblyai);
        if (k.gemini)     fd.append("gemini_api_key", k.gemini);
        if (k.murf)       fd.append("murf_api_key", k.murf);
        if (k.tavily)     fd.append("tavily_api_key", k.tavily);

        const res = await fetch(`/agent/chat/${sessionId}`, { method: "POST", body: fd });
        const js = await res.json();

        if (!res.ok) throw new Error(js.error || "Server error");

        // Show bot text
        addMessage(js.llm_text || "(no response)", "left");

        // Play bot audio (or fallback)
        aiAudio.src = js.audio_url || "/static/fallback.mp3";
        try { await aiAudio.play(); } catch {}
        aiAudio.onended = () => {
          // auto-listen again
          startRecording().catch(() => {});
        };
      } catch (err) {
        addMessage("I'm having trouble connecting right now. Please try again.", "left");
        aiAudio.src = "/static/fallback.mp3";
        try { await aiAudio.play(); } catch {}
      }
    };

    mediaRecorder.start();
    isRecording = true;
    recordBtn.classList.add("pulse");
    recordBtn.setAttribute("aria-pressed", "true");
    statusText.textContent = "Listening… tap to stop";
    showToast("Recording started");
  } catch (err) {
    showToast("Mic error: " + err.message);
  }
}

async function stopRecording() {
  if (mediaRecorder && isRecording) {
    mediaRecorder.stop();
    isRecording = false;
    recordBtn.classList.remove("pulse");
    recordBtn.setAttribute("aria-pressed", "false");
    statusText.textContent = "Thinking…";
    showToast("Recording stopped");
  }
}

// Toggle on button click + keyboard Enter/Space
recordBtn.addEventListener("click", () => {
  if (isRecording) stopRecording(); else startRecording();
});
recordBtn.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    recordBtn.click();
  }
});

// Make sure Session ID appears immediately on load (in case JS loads before DOM ready)
document.addEventListener("DOMContentLoaded", () => {
  const el = document.getElementById("sessionIdText");
  if (el && !el.textContent) el.textContent = sessionId;
});
// ========== Turn Detection (from /ws/transcribe) ==========

// WebSocket connection for live transcription + turn detection
let turnSocket;
function initTurnSocket() {
  try {
    const url = `ws://${window.location.host}/ws/transcribe?session_id=${encodeURIComponent(sessionId)}`;
    turnSocket = new WebSocket(url);

    turnSocket.onopen = () => {
      console.log("[TurnWS] open");
    };

    turnSocket.onmessage = (evt) => {
      // server may send text or JSON
      let payload = null;
      try {
        payload = JSON.parse(evt.data);
      } catch {
        // not JSON; ignore
        return;
      }

      if (payload && payload.type === "turn" && payload.end_of_turn) {
        // reveal the panel on first turn
        const box = document.getElementById("turnBox");
        if (box.classList.contains("hidden")) box.classList.remove("hidden");

        const log = document.getElementById("turnLog");
        const row = document.createElement("div");
        row.className = "bg-white/5 border border-white/10 rounded-lg px-3 py-2";
        row.textContent = payload.transcript || "(no transcript)";
        log.appendChild(row);
        log.scrollTop = log.scrollHeight;
      }
    };

    turnSocket.onerror = (e) => {
      console.warn("[TurnWS] error", e);
    };

    turnSocket.onclose = () => {
      console.log("[TurnWS] closed");
      // Optional: simple retry if you want it persistent
      // setTimeout(initTurnSocket, 2000);
    };
  } catch (err) {
    console.error("[TurnWS] cannot init:", err);
  }
}

document.addEventListener("DOMContentLoaded", initTurnSocket);

// ---------------- Your existing mic controls ----------------
const micButton = document.getElementById("micButton");
const micStatus = document.getElementById("micStatus");
document.addEventListener("DOMContentLoaded", initTurnSocket);

let isListening = false;
micButton?.addEventListener("click", () => {
  // Keep your existing start/stop logic; only update label here
  isListening = !isListening;
  micStatus.textContent = isListening ? "Listening… tap to stop" : "Tap to start recording";
});

// ===== Doraemon GIF state control (ADD-ONLY) =====
(function () {
  const img        = document.getElementById("doraemonImg");
  const recordBtn  = document.getElementById("recordBtn");
  const statusText = document.getElementById("statusText");
  const ai         = document.getElementById("aiAudio");
  if (!img) return; // nothing to do if the image isn't in the DOM

  const SRC = {
    idle: "/static/doraemon_idle.gif",
    listening: "/static/doraemon_listening.gif",
    talking: "/static/doraemon_talking.gif",
  };

  // cache-bust to force reload so you actually see swaps
  const bust = (url) => url + (url.includes("?") ? "&" : "?") + "t=" + Date.now();
  const swap = (mode) => {
    const url = SRC[mode] || SRC.idle;
    img.onerror = () => console.warn("[Doraemon] Missing image:", url);
    img.src = bust(url);
  };

  // --- Signal A: direct pointer interaction on the mic button
  if (recordBtn) {
    recordBtn.addEventListener("pointerdown", () => swap("listening"));
    recordBtn.addEventListener("pointerup",   () => swap("idle"));
    recordBtn.addEventListener("mouseleave",  () => swap("idle"));

    // --- Signal B: watch aria-pressed changes (if your code toggles it)
    new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.type === "attributes" && m.attributeName === "aria-pressed") {
          const pressed = recordBtn.getAttribute("aria-pressed") === "true";
          swap(pressed ? "listening" : "idle");
        }
      }
    }).observe(recordBtn, { attributes: true });
  }

  // --- Signal C: watch status text content changes (very robust)
  // Triggers "listening" when the text contains typical words; otherwise defaults to idle
  if (statusText) {
    const toModeFromText = (txt) => {
      const t = (txt || "").toLowerCase();
      if (t.includes("listening") || t.includes("recording") || t.includes("speak")) return "listening";
      return "idle";
    };
    new MutationObserver(() => swap(toModeFromText(statusText.textContent)))
      .observe(statusText, { childList: true, subtree: true, characterData: true });
  }

  // --- Signal D: AI TTS playback means "talking"
  if (ai) {
    ai.addEventListener("play",  () => swap("talking"));
    ai.addEventListener("ended", () => swap("idle"));
    ai.addEventListener("pause", () => { if (!ai.ended) swap("idle"); });
    ai.addEventListener("error", () => swap("idle"));
  }

  // start in idle
  swap("idle");
})();

(function () {
  const modal = document.getElementById("configModal");
  const open  = document.getElementById("openConfig");
  const close = document.getElementById("closeConfig");
  const cancel= document.getElementById("cancelConfig");
  const save  = document.getElementById("saveConfig");

  const fields = {
    assemblyai: document.getElementById("assemblyaiKey"),
    gemini:     document.getElementById("geminiKey"),
    murf:       document.getElementById("murfKey"),
    tavily:     document.getElementById("tavilyKey"),
  };

  const fill = () => {
    fields.assemblyai.value = localStorage.getItem("key_assemblyai") || "";
    fields.gemini.value     = localStorage.getItem("key_gemini")     || "";
    fields.murf.value       = localStorage.getItem("key_murf")       || "";
    fields.tavily.value     = localStorage.getItem("key_tavily")     || "";
  };

  const show = () => { fill(); modal.classList.remove("hidden"); modal.classList.add("flex"); };
  const hide = () => { modal.classList.add("hidden"); modal.classList.remove("flex"); };

  open?.addEventListener("click", show);
  close?.addEventListener("click", hide);
  cancel?.addEventListener("click", hide);
  save?.addEventListener("click", () => {
    localStorage.setItem("key_assemblyai", fields.assemblyai.value.trim());
    localStorage.setItem("key_gemini",     fields.gemini.value.trim());
    localStorage.setItem("key_murf",       fields.murf.value.trim());
    localStorage.setItem("key_tavily",     fields.tavily.value.trim());
    hide();
    try { showToast("Saved API keys"); } catch {}
  });
})();