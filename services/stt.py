import os
import assemblyai as aai


#aai.settings.api_key = os.getenv("ASSEMBLYAI_API_KEY")

def transcribe_tempfile(path: str) -> str:
    transcriber = aai.Transcriber()
    result = transcriber.transcribe(path)
    return (result.text or "")
