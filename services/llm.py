import os
import requests
import json
from typing import Dict, List

# REST endpoint for Gemini (no SDK usage)
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/"
    "v1beta/models/gemini-2.5-pro:generateContent"
)

def _extract_text_from_candidates(resp_json: Dict) -> str:
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

def _generate_contents(contents: List[Dict], system_instruction: str | None = None) -> str:
    """
    Call Gemini REST with a `contents` array (multi-turn).
    contents example:
      [{"role":"user","parts":[{"text": "Hello"}]},
       {"role":"model","parts":[{"text":"Hi!"}]}]
    """
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY missing")

    body: Dict = {"contents": contents}
    if system_instruction:
        body["systemInstruction"] = {
            "role": "system",
            "parts": [{"text": system_instruction}]
        }

    params = {"key": api_key}
    headers = {"Content-Type": "application/json"}

    r = requests.post(GEMINI_ENDPOINT, params=params, headers=headers, json=body, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text}")

    data = r.json()
    # print("[DEBUG] Gemini raw:", json.dumps(data, indent=2)[:2000])
    return _extract_text_from_candidates(data)

def chat_from_history(history: List[Dict[str, str]]) -> str:
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

    # First, try multi-turn
    try:
        text = _generate_contents(contents, system_instruction=system_msg)
        if text.strip():
            return text.strip()
    except Exception:
        pass

    # Fallback: last user-only prompt
    last_user = ""
    for turn in reversed(history):
        if turn["author"] == "user":
            last_user = turn["content"]
            break

    text = _generate_contents(
        [{"role": "user", "parts": [{"text": last_user or "Hello"}]}],
        system_instruction=system_msg
    )
    return text.strip()
