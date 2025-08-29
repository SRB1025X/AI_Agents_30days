from fastapi import FastAPI, Request, UploadFile, File, Form
import requests, json
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from typing import Dict, List
import os, datetime
from datetime import datetime
import assemblyai as aai
from fastapi import WebSocket, WebSocketDisconnect, Query
from assemblyai import RealtimeTranscriber, RealtimeTranscript
import threading
import logging
from queue import Queue
import queue
import time
from typing import Type
import threading
import queue
from fastapi import WebSocket, WebSocketDisconnect, Query
import assemblyai as aai
from assemblyai.streaming.v3 import (
    BeginEvent,
    StreamingClient,
    StreamingClientOptions,
    StreamingError,
    StreamingEvents,
    StreamingParameters,
    StreamingSessionParameters,
    TerminationEvent,
    TurnEvent,
)
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
from services.stream import router as stream_router

from tempfile import NamedTemporaryFile

# ------------------------------------------------------------------------------
# App setup
# ------------------------------------------------------------------------------
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
app.include_router(stream_router)

# Load env
load_dotenv()
MURF_API_KEY = os.getenv("MURF_API_KEY")
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
aai.settings.api_key = ASSEMBLYAI_API_KEY
FALLBACK_AUDIO_URL = "/static/fallback.mp3"

os.makedirs("uploads", exist_ok=True)

log = get_logger(__name__)

# ------------------------------------------------------------------------------
# In-memory chat history store: { session_id: [ {author, content}, ... ] }
# ------------------------------------------------------------------------------
chat_history: Dict[str, List[Dict[str, str]]] = {}
def _stamp():
    return datetime.now().strftime("%H:%M:%S")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
tavily_log = logging.getLogger("tavily")

def _redact(s: str, max_len: int = 300) -> str:
    s = (s or "").strip()
    return s[:max_len] + ("…" if len(s) > max_len else "")

def tavily_search_brief(query: str, api_key: str | None = None) -> str:
    """
    Ask Tavily for a concise answer/context we can pass to Gemini.
    Logs request/response details to the terminal.
    """
    api_key = api_key or os.getenv("TAVILY_API_KEY")
    if not api_key:
        tavily_log.error("missing_api_key")
        raise RuntimeError("TAVILY_API_KEY missing")

    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "advanced",
        "include_answer": True,
        "max_results": 5,
    }

    t0 = time.perf_counter()
    tavily_log.info("request %s", json.dumps({
        "query_excerpt": _redact(query, 200),
        "search_depth": payload["search_depth"],
        "max_results": payload["max_results"],
    }, ensure_ascii=False))

    try:
        r = requests.post("https://api.tavily.com/search", json=payload, timeout=30)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        tavily_log.info("response %s", json.dumps({
            "status": r.status_code,
            "elapsed_ms": elapsed_ms,
        }, ensure_ascii=False))

        # Raise for non-200s so we hit the except with an error log
        r.raise_for_status()

        data = r.json()
        answer = (data.get("answer") or "").strip()
        results = data.get("results") or []

        # Debug-level rich info
        if tavily_log.isEnabledFor(logging.DEBUG):
            tavily_log.debug("answer_excerpt %s", json.dumps({
                "excerpt": _redact(answer, 500)
            }, ensure_ascii=False))
            tavily_log.debug("top_sources %s", json.dumps({
                "count": len(results),
                "top3": [
                    {
                        "title": _redact((res.get("title") or "").strip(), 120),
                        "url": _redact((res.get("url") or "").strip(), 200),
                    } for res in results[:3]
                ],
            }, ensure_ascii=False))

        # Fallback if no direct answer
        if not answer:
            snippets = []
            for res in results[:3]:
                c = (res.get("content") or "").strip()
                if c:
                    snippets.append(_redact(c, 300))
            answer = "\n\n".join(snippets).strip() or "No useful web context found."

        return answer

    except requests.RequestException as e:
        # Log body if available (at DEBUG to avoid noisy prod logs)
        body = None
        try:
            body = r.text  # may not exist if request itself failed
        except Exception:
            pass
        tavily_log.error("http_error %s", json.dumps({
            "error": str(e),
            "response_excerpt": _redact(body or "", 500),
        }, ensure_ascii=False))
        raise

    except Exception as e:
        tavily_log.error("unexpected_error %s", json.dumps({
            "error": str(e)
        }, ensure_ascii=False))
        raise

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
async def agent_chat(
    session_id: str,
    file: UploadFile = File(...),
    web_search: bool = Form(False),
    concise: bool = Form(False),
    assemblyai_api_key: str | None = Form(None),
    gemini_api_key: str | None = Form(None),
    murf_api_key: str | None = Form(None),
    tavily_api_key: str | None = Form(None),
):
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
            transcript = transcribe_tempfile(
                tmp_path,
                api_key=assemblyai_api_key or ASSEMBLYAI_API_KEY
            ).strip()
            if not transcript:
                raise RuntimeError("Empty transcript")
        except Exception as e:
            log.warning("STT error on session %s: %s", session_id, e)
            return JSONResponse(status_code=502, content=error_json("stt", e))

        # Concise mode log (mirrors "mode web_search_*" logs)
        log.info("mode concise_on" if concise else "mode concise_off")
        # History
        history = chat_history.setdefault(session_id, [])

        # If toggle is ON, insert a single web step between STT and LLM
        if web_search:
            tavily_log.info("mode web_search_on")
            try:
                web_ctx = tavily_search_brief(transcript, api_key=tavily_api_key)
                enriched = (
                    "Use the following web context to answer briefly. "
                    "Do not show raw URLs; refer to sources naturally.\n\n"
                    f"[Web context]\n{web_ctx}\n\n"
                    f"[User question]\n{transcript}"
                )
                user_content = enriched
                if concise:
                    user_content = (
                        "Answer in exactly 3 short bullet points. Keep it under 60 words.\n\n"
                        + enriched
                    )
                history.append({"author": "user", "content": user_content})
            except Exception as e:
                # If Tavily fails, gracefully fall back to original behavior
                user_content = transcript
                if concise:
                    user_content = (
                        "Answer in exactly 3 short bullet points. Keep it under 60 words.\n\n"
                        f"[User question]\n{transcript}"
                    )
                history.append({"author": "user", "content": user_content})
                log.warning("Tavily error on session %s: %s", session_id, e)
        else:
            tavily_log.info("mode web_search_off")
            user_content = transcript
            if concise:
                user_content = (
                    "Answer in exactly 3 short bullet points. Keep it under 60 words. return text without any formatting\n\n"
                    f"[User question]\n{transcript}"
                )
            history.append({"author": "user", "content": user_content})

        # LLM
        try:
            log.info(
                "keys_used aai=%s gemini=%s murf=%s tavily=%s",
                "user" if assemblyai_api_key else "env",
                "user" if gemini_api_key else "env",
                "user" if murf_api_key else "env",
                "user" if tavily_api_key else "env",
            )
            llm_text = chat_from_history(history, api_key=gemini_api_key).strip()
            if not llm_text:
                llm_text = "Let's talk about something else. How can I help you today?"
        except Exception as e:
            log.warning("LLM error on session %s: %s", session_id, e)
            warn = error_json("llm", e, {"transcript": transcript})
            ok, audio_url, warn = try_murf_tts(llm_text, murf_api_key or MURF_API_KEY)
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

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        await ws.send_text('👋 Connected to /ws. Send any text and I will echo it back!')
        while True:
            msg = await ws.receive_text()              # receive a text frame
            await ws.send_text(f"🔁 echo: {msg}")      # send it back
    except WebSocketDisconnect:
        # client closed the connection
        # (optional) log here if you like
        pass
    except Exception as e:
        # (optional) send an error frame before closing
        try:
            await ws.send_text(f"❌ server error: {type(e).__name__}: {e}")
        except Exception:
            pass
        # then close (1000 = normal closure)
        try:
            await ws.close(code=1000)
        except Exception:
            pass

def _stamp():
    import datetime as _dt
    return _dt.datetime.now().strftime("%H:%M:%S")

def on_begin(self: Type[StreamingClient], event: BeginEvent):
    print(f"Session started: {event.id}")

def on_turn(self: Type[StreamingClient], event: TurnEvent):
    print(f"{event.transcript} ({event.end_of_turn})")

    # Notify connected websocket client (if available)
    try:
        if event.end_of_turn:
            # broadcast to client: {"type":"turn","transcript":"...","end_of_turn":true}
            ws_msg = {"type": "turn", "transcript": event.transcript, "end_of_turn": True}
            # you already have the `websocket` object in ws_transcribe
            import json, asyncio
            asyncio.create_task(websocket.send_text(json.dumps(ws_msg)))
    except Exception as e:
        print("WS send error:", e)

    if event.end_of_turn and not event.turn_is_formatted:
        params = StreamingSessionParameters(format_turns=True)
        self.set_params(params)
    

def on_terminated(self: Type[StreamingClient], event: TerminationEvent):
    print(f"Session terminated: {event.audio_duration_seconds} seconds of audio processed")

def on_error(self: Type[StreamingClient], error: StreamingError):
    print(f"Error occurred: {error}")


# ---------- WebSocket endpoint that bridges browser -> AAI StreamingClient ----------
@app.websocket("/ws/transcribe")
async def ws_transcribe(websocket: WebSocket, session_id: str = Query(default="no-session")):
    """
    Expect raw PCM16 mono @ 16 kHz bytes over the websocket.
    Sends partial/final transcripts to terminal.
    Send a text frame '__end__' (or close) to finish.
    """
    await websocket.accept()
    print("[WS] client connected; session:", session_id)

    client = StreamingClient(
        StreamingClientOptions(
            api_key=aai.settings.api_key,
            api_host="streaming.assemblyai.com",
        )
    )
    client.on(StreamingEvents.Begin, on_begin)
    client.on(StreamingEvents.Turn, on_turn)
    client.on(StreamingEvents.Termination, on_terminated)
    client.on(StreamingEvents.Error, on_error)

    # Connect AAI session
    client.connect(
        StreamingParameters(
            sample_rate=16000,
            format_turns=True,  # prints cleaner, formatted turns on end_of_turn
        )
    )

    # ---- WS → AAI queue bridge ----
    audio_q: "queue.Queue[bytes | None]" = queue.Queue()

    def byte_iter():
        """Yield PCM16 chunks pushed by the websocket; stop on None sentinel."""
        while True:
            chunk = audio_q.get()
            if chunk is None:
                break
            if chunk:
                yield chunk

    def _stream_worker():
        try:
            client.stream(byte_iter())
        finally:
            # ensure session closes
            try:
                client.disconnect(terminate=True)
            except Exception:
                pass

    t = threading.Thread(target=_stream_worker, daemon=True)
    t.start()

    try:
        while True:
            msg = await websocket.receive()
            if "bytes" in msg and msg["bytes"] is not None:
                # push raw PCM16 to queue
                audio_q.put(msg["bytes"])
            elif "text" in msg and msg["text"] == "__end__":
                # flush/end
                audio_q.put(None)
                break
            elif msg.get("type") in ("websocket.disconnect", "websocket.close"):
                audio_q.put(None)
                break
    except WebSocketDisconnect:
        audio_q.put(None)
    except Exception as e:
        print("[WS] error:", e)
        audio_q.put(None)
    finally:
        try:
            t.join(timeout=0.5)
        except Exception:
            pass
        try:
            client.disconnect(terminate=True)
        except Exception:
            pass
        print("[WS] connection closed")

@app.get("/test", response_class=HTMLResponse)
async def serve_test(request: Request):
    return templates.TemplateResponse("streaming-test.html", {"request": request})

'''@app.websocket("/ws/stream")
async def ws_stream(
    websocket: WebSocket,
    session_id: str = Query(default="no-session")
):
    # 1) Accept the WS *before* any receive or you’ll get 403
    await websocket.accept()
    await websocket.send_text(json.dumps({"type": "ACK", "detail": "accepted"}))

    # 2) Prepare file path
    ts = int(time.time() * 1000)
    filename = f"uploads/stream_{session_id}_{ts}.webm"
    f = open(filename, "wb")
    total = 0

    try:
        while True:
            msg = await websocket.receive()

            # JSON control messages arrive as text
            if "text" in msg and msg["text"] is not None:
                try:
                    data = json.loads(msg["text"])
                except Exception:
                    await websocket.send_text(json.dumps({"type": "ERROR", "error": "bad JSON"}))
                    continue

                t = data.get("type")
                if t == "START":
                    await websocket.send_text(json.dumps({"type": "ACK", "detail": "recording"}))
                elif t == "STOP":
                    # flush & confirm
                    f.flush(); f.close()
                    await websocket.send_text(json.dumps({
                        "type": "SAVED",
                        "filename": filename,
                        "bytes": total
                    }))
                    await websocket.close()
                    return
                else:
                    await websocket.send_text(json.dumps({"type":"ACK","detail":f"unknown:{t}"}))
                continue

            # Binary chunks (ArrayBuffer) land in "bytes"
            if "bytes" in msg and msg["bytes"] is not None:
                chunk = msg["bytes"]
                f.write(chunk)
                total += len(chunk)
                # keep the loop tight; a periodic ack is optional
                if total % (256*1024) < 4096:  # ~every 256KB
                    await websocket.send_text(json.dumps({"type": "ACK", "detail": f"bytes={total}"}))

            # Handle close frames
            if msg.get("type") == "websocket.disconnect":
                break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(json.dumps({"type":"ERROR","error":str(e)}))
        except Exception:
            pass
    finally:
        try: f.close()
        except Exception: pass
        '''