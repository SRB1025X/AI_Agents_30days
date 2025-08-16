from pydantic import BaseModel, Field
from typing import Optional, Dict, Any

# ---------- Common error shape ----------
def error_json(stage: str, exc: Exception, extra: dict | None = None):
    payload = {
        "ok": False,
        "stage": stage,
        "error": f"{type(exc).__name__}: {str(exc)}"
    }
    if extra:
        payload.update(extra)
    return payload

# ---------- Requests ----------
class TextInput(BaseModel):
    text: str = Field(..., min_length=1)

# ---------- Responses ----------
class GenerateAudioResponse(BaseModel):
    ok: bool
    audio_url: str
    fallback: Optional[bool] = False
    warning: Optional[Dict[str, Any]] = None

class UploadResponse(BaseModel):
    ok: bool
    filename: str
    content_type: str
    size_kb: float

class TranscribeResponse(BaseModel):
    ok: bool
    transcript: str

class ChatResponse(BaseModel):
    ok: bool
    transcript: str
    llm_text: str
    audio_url: str
    fallback: Optional[bool] = False
    warning: Optional[Dict[str, Any]] = None
