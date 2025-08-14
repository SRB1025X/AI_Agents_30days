from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import FastAPI, Request, UploadFile, File
from dotenv import load_dotenv
from pydantic import BaseModel
import os, requests, traceback, json
from tempfile import NamedTemporaryFile
import assemblyai as aai
from typing import List, Dict

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Serve index.html at /
@app.get("/", response_class=HTMLResponse)
async def serve_home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# Load .env variables
#load_dotenv()

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".gitignore", ".env"))
MURF_API_KEY = os.getenv("MURF_API_KEY")
aai.settings.api_key = os.getenv("ASSEMBLYAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # keep API key in .env

FALLBACK_AUDIO_URL = "/static/fallback.mp3"

# --------------------------- Murf helper + error shape -------------------------

def error_json(stage: str, exc: Exception, extra: dict | None = None):
    payload = {
        "ok": False,
        "stage": stage,
        "error": f"{type(exc).__name__}: {str(exc)}"
    }
    if extra:
        payload.update(extra)
    return payload

def try_murf_tts(text: str, murf_api_key: str) -> tuple[bool, str | None, dict | None]:
    """
    Returns (ok, audio_url, error_info)
    ok=False means you should play the fallback audio.
    """
    try:
        if not murf_api_key:
            raise RuntimeError("MURF_API_KEY missing")
        r = requests.post(
            "https://api.murf.ai/v1/speech/generate",
            headers={"api-key": murf_api_key, "Content-Type": "application/json"},
            json={"text": text[:3000], "voice_id": "en-US-natalie", "format": "mp3"},
            timeout=30
        )
        if r.status_code != 200:
            raise RuntimeError(f"Murf HTTP {r.status_code}: {r.text}")
        audio_url = r.json().get("audioFile")
        if not audio_url:
            raise RuntimeError("Murf response missing audioFile")
        return True, audio_url, None
    except Exception as e:
        return False, None, error_json("tts", e)

# --------------------------- Gemini REST (no SDK) ------------------------------

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/"
    "v1beta/models/gemini-2.5-pro:generateContent"
)

def _extract_text_from_candidates(resp_json: dict) -> str:
    """
    Safely pull plain text from Gemini REST response. Returns "" if none.
    """
    try:
        cands = resp_json.get("candidates") or []
        for c in cands:
            content = c.get("content") or {}
            parts = content.get("parts") or []
            texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
            combined = " ".join(t for t in texts if t).strip()
            if combined:
                return combined
        return ""
    except Exception:
        return ""

def gemini_generate_contents(contents: List[Dict], system_instruction: str | None = None) -> str:
    """
    Call Gemini REST with a `contents` array (multi-turn).
    contents example:
      [{"role":"user","parts":[{"text": "Hello"}]},
       {"role":"model","parts":[{"text":"Hi!"}]}]
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing")

    body: Dict = {"contents": contents}
    if system_instruction:
        body["systemInstruction"] = {
            "role": "system",
            "parts": [{"text": system_instruction}]
        }

    params = {"key": GEMINI_API_KEY}
    headers = {"Content-Type": "application/json"}

    r = requests.post(GEMINI_ENDPOINT, params=params, headers=headers, json=body, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text}")

    data = r.json()
    # Uncomment for debugging:
    # print("[DEBUG] Gemini raw:", json.dumps(data, indent=2)[:2000])
    text = _extract_text_from_candidates(data)
    return text

def gemini_chat_from_history(history: List[Dict[str, str]]) -> str:
    """
    Convert in-memory history into Gemini `contents`, then call REST.
    history: [{"author":"user","content":"..."}, {"author":"assistant","content":"..."}]
    """
    role_map = {"user": "user", "assistant": "model"}
    contents: List[Dict] = []
    for turn in history:
        role = role_map.get(turn["author"], "user")
        contents.append({"role": role, "parts": [{"text": turn["content"]}]})

    system_msg = (
        "You are a concise, friendly assistant. "
        "Always respond safely and helpfully in plain text."
    )

    # First try multi-turn
    try:
        text = gemini_generate_contents(contents, system_instruction=system_msg)
        if text.strip():
            return text.strip()
    except Exception as e:
        # print("[DEBUG] Gemini multi-turn error:", e)
        pass

    # Fallback: last user message only (can dodge some safety blocks)
    last_user = ""
    for turn in reversed(history):
        if turn["author"] == "user":
            last_user = turn["content"]
            break

    text = gemini_generate_contents(
        [{"role": "user", "parts": [{"text": last_user or "Hello"}]}],
        system_instruction=system_msg
    )
    return text.strip()

# --------------------------- FastAPI models & routes ---------------------------

class TextInput(BaseModel):
    text: str

@app.post("/generate-audio")
def generate_audio(input: TextInput):
    """Single-turn TTS helper with fallback."""
    try:
        ok, audio_url, err = try_murf_tts(input.text, MURF_API_KEY)
        if ok:
            return {"ok": True, "audio_url": audio_url}
        # Fallback path (still 200 so the UI can play something)
        return {"ok": True, "audio_url": FALLBACK_AUDIO_URL, "fallback": True, "warning": err}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content=error_json("tts", e))

@app.post("/upload-audio")
async def upload_audio(file: UploadFile = File(...)):
    try:
        upload_path = f"uploads/{file.filename}"
        os.makedirs("uploads", exist_ok=True)
        with open(upload_path, "wb") as buffer:
            buffer.write(await file.read())
        return {
            "ok": True,
            "filename": file.filename,
            "content_type": file.content_type,
            "size_kb": round(os.path.getsize(upload_path) / 1024, 2)
        }
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content=error_json("upload", e))

@app.post("/transcribe/file")
async def transcribe_file(file: UploadFile = File(...)):
    """STT with structured errors."""
    try:
        with NamedTemporaryFile(delete=False, suffix=".webm") as temp_file:
            temp_file.write(await file.read())
            temp_path = temp_file.name

        try:
            transcriber = aai.Transcriber()
            transcript = transcriber.transcribe(temp_path)
            text = (transcript.text or "").strip()
            if not text:
                raise RuntimeError("Empty transcript")
            return {"ok": True, "transcript": text}
        finally:
            try:
                os.remove(temp_path)
            except Exception:
                pass

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content=error_json("stt", e))

# In-memory chat history: { session_id: [ {author, content}, … ] }
chat_history: dict[str, list[dict[str, str]]] = {}

@app.post("/agent/chat/{session_id}")
async def agent_chat(session_id: str, file: UploadFile = File(...)):
    """
    Voice -> STT -> append to history -> LLM (Gemini REST) -> TTS (Murf, with fallback).
    """
    tmp_path = None
    try:
        # 1) Save incoming audio to a temp .webm
        with NamedTemporaryFile(delete=False, suffix=".webm") as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        # 2) Transcribe via AssemblyAI
        try:
            transcript = aai.Transcriber().transcribe(tmp_path).text or ""
            transcript = transcript.strip()
            if not transcript:
                raise RuntimeError("Empty transcript")
        except Exception as e:
            return JSONResponse(status_code=502, content=error_json("stt", e))

        # 3) Manage in-memory history
        history = chat_history.setdefault(session_id, [])
        history.append({"author": "user", "content": transcript})

        # 4) Call Gemini REST using multi-turn contents
        try:
            llm_text = gemini_chat_from_history(history)
            if not llm_text:
                llm_text = "Let's talk about something else. How can I help you today?"
        except Exception as e:
            # LLM hard-failed → speak friendly fallback
            warn = error_json("llm", e, {"transcript": transcript})
            ok, audio_url, _ = try_murf_tts(
                "I'm having trouble connecting right now. Please try again.",
                MURF_API_KEY
            )
            return {
                "ok": True,
                "transcript": transcript,
                "llm_text": "I'm having trouble connecting right now. Please try again.",
                "audio_url": audio_url if ok else FALLBACK_AUDIO_URL,
                "fallback": True,
                "warning": warn,
            }

        # Persist assistant turn
        history.append({"author": "assistant", "content": llm_text})

        # 5) Synthesize LLM reply with Murf (with fallback)
        ok, audio_url, err = try_murf_tts(llm_text, MURF_API_KEY)
        if not ok:
            return {
                "ok": True,
                "transcript": transcript,
                "llm_text": llm_text,
                "audio_url": FALLBACK_AUDIO_URL,
                "fallback": True,
                "warning": err
            }

        # 6) Return success
        return {"ok": True, "transcript": transcript, "llm_text": llm_text, "audio_url": audio_url}

    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
