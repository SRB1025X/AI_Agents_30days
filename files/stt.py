import os
import assemblyai as aai
from typing import Optional


#aai.settings.api_key = os.getenv("ASSEMBLYAI_API_KEY")

def transcribe_tempfile(path: str, api_key: Optional[str] = None) -> str:
    if api_key:
        aai.settings.api_key = api_key
    transcriber = aai.Transcriber()
    result = transcriber.transcribe(path)
    return (result.text or "")
