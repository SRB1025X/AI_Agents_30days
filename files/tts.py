import requests
from typing import Tuple, Optional, Dict, Any

def try_murf_tts(text: str, murf_api_key: str) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    try:
        if not murf_api_key:
            raise RuntimeError("MURF_API_KEY missing")
        r = requests.post(
            "https://api.murf.ai/v1/speech/generate",
            headers={"api-key": murf_api_key, "Content-Type": "application/json"},
            json={"text": text[:3000], "voice_id": "en-US-miles", "style": "Conversational", "pitch": 1.7, "speed": 1.9, "format": "mp3"},
            timeout=30
        )
        if r.status_code != 200:
            raise RuntimeError(f"Murf HTTP {r.status_code}: {r.text}")
        audio_url = r.json().get("audioFile")
        if not audio_url:
            raise RuntimeError("Murf response missing audioFile")
        return True, audio_url, None
    except Exception as e:
        return False, None, {
            "ok": False,
            "stage": "tts",
            "error": f"{type(e).__name__}: {str(e)}"
        }
