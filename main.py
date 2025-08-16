from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from typing import Dict, List
import os
import traceback
import assemblyai as aai

from models import (
    TextInput,
    GenerateAudioResponse,
    UploadResponse,
    TranscribeResponse,
    ChatResponse,
    error_json,
)
from services.stt import transcribe_tempfile
from services.llm import chat_from_history
from services.tts import try_murf_tts
from utils.logging_config import get_logger

from tempfile import NamedTemporaryFile

# ------------------------------------------------------------------------------
# App setup
# ------------------------------------------------------------------------------
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Load env
load_dotenv()
MURF_API_KEY = os.getenv("MURF_API_KEY")
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
aai.settings.api_key = ASSEMBLYAI_API_KEY
FALLBACK_AUDIO_URL = "/static/fallback.mp3"

log = get_logger(__name__)

# ------------------------------------------------------------------------------
# In-memory chat history store: { session_id: [ {author, content}, ... ] }
# ------------------------------------------------------------------------------
chat_history: Dict[str, List[Dict[str, str]]] = {}

# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_home(request: Request):
    """Serve the single-page voice agent UI."""
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/generate-audio", response_model=GenerateAudioResponse)
def generate_audio(input: TextInput):
    """
    Single-turn TTS helper with fallback. Keeps response shape stable for the UI.
    """
    try:
        ok, audio_url, warn = try_murf_tts(input.text, MURF_API_KEY)
        if ok:
            return GenerateAudioResponse(ok=True, audio_url=audio_url)
        # Fallback still returns 200 so client can play something
        return GenerateAudioResponse(ok=True, audio_url=FALLBACK_AUDIO_URL, fallback=True, warning=warn)
    except Exception as e:
        log.exception("TTS failure")
        return JSONResponse(status_code=500, content=error_json("tts", e))

@app.post("/upload-audio", response_model=UploadResponse)
async def upload_audio(file: UploadFile = File(...)):
    """Simple file upload; useful for debugging and day 5 behavior."""
    try:
        os.makedirs("uploads", exist_ok=True)
        save_path = os.path.join("uploads", file.filename)
        with open(save_path, "wb") as f:
            f.write(await file.read())
        size_kb = round(os.path.getsize(save_path) / 1024, 2)
        return UploadResponse(ok=True, filename=file.filename, content_type=file.content_type, size_kb=size_kb)
    except Exception as e:
        log.exception("Upload failed")
        return JSONResponse(status_code=500, content=error_json("upload", e))

@app.post("/transcribe/file", response_model=TranscribeResponse)
async def transcribe_file(file: UploadFile = File(...)):
    """Speech-to-text with AssemblyAI; returns transcript or structured error."""
    tmp_path = None
    try:
        with NamedTemporaryFile(delete=False, suffix=".webm") as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        text = transcribe_tempfile(tmp_path).strip()
        if not text:
            raise RuntimeError("Empty transcript")
        return TranscribeResponse(ok=True, transcript=text)
    except Exception as e:
        log.exception("STT failure")
        return JSONResponse(status_code=500, content=error_json("stt", e))
    finally:
        if tmp_path:
            try: os.remove(tmp_path)
            except Exception: pass

@app.post("/agent/chat/{session_id}", response_model=ChatResponse)
async def agent_chat(session_id: str, file: UploadFile = File(...)):
    """
    Voice -> STT -> append to history -> LLM (Gemini REST) -> TTS (Murf, with fallback).
    Robust error handling at each stage.
    """
    tmp_path = None
    try:
        # Save incoming audio
        with NamedTemporaryFile(delete=False, suffix=".webm") as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        # STT
        try:
            transcript = transcribe_tempfile(tmp_path).strip()
            if not transcript:
                raise RuntimeError("Empty transcript")
        except Exception as e:
            log.warning("STT error on session %s: %s", session_id, e)
            return JSONResponse(status_code=502, content=error_json("stt", e))

        # History
        history = chat_history.setdefault(session_id, [])
        history.append({"author": "user", "content": transcript})

        # LLM
        try:
            llm_text = chat_from_history(history).strip()
            if not llm_text:
                llm_text = "Let's talk about something else. How can I help you today?"
        except Exception as e:
            log.warning("LLM error on session %s: %s", session_id, e)
            warn = error_json("llm", e, {"transcript": transcript})
            ok, audio_url, _ = try_murf_tts(
                "I'm having trouble connecting right now. Please try again.",
                MURF_API_KEY
            )
            return ChatResponse(
                ok=True,
                transcript=transcript,
                llm_text="I'm having trouble connecting right now. Please try again.",
                audio_url=audio_url if ok else FALLBACK_AUDIO_URL,
                fallback=True,
                warning=warn,
            )

        # Persist assistant response
        history.append({"author": "assistant", "content": llm_text})

        # TTS
        ok, audio_url, warn = try_murf_tts(llm_text, MURF_API_KEY)
        if not ok:
            return ChatResponse(
                ok=True,
                transcript=transcript,
                llm_text=llm_text,
                audio_url=FALLBACK_AUDIO_URL,
                fallback=True,
                warning=warn
            )

        return ChatResponse(ok=True, transcript=transcript, llm_text=llm_text, audio_url=audio_url)

    except Exception as e:
        log.exception("Agent pipeline failure")
        return JSONResponse(status_code=500, content=error_json("agent", e))
    finally:
        if tmp_path:
            try: os.remove(tmp_path)
            except Exception: pass
