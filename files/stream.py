# services/stream.py
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
import os, time, json
from typing import Optional

router = APIRouter()

# Ensure uploads directory exists (safe if already present)
os.makedirs("uploads", exist_ok=True)

@router.websocket("/ws/stream")
async def ws_stream(
    websocket: WebSocket,
    session_id: str = Query(default="no-session")
):
    """
    Simple streaming endpoint:
    - Client sends a JSON text message {"type":"START"}
    - Client streams audio binary frames (WebSocket bytes)
    - Client sends a JSON text message {"type":"STOP"} to finalize
    We just save all bytes to /uploads/stream_{session_id}_{ts}.webm
    """
    await websocket.accept()
    await websocket.send_text(json.dumps({"type": "ACK", "detail": "accepted"}))

    ts = int(time.time() * 1000)
    filename = f"uploads/stream_{session_id}_{ts}.webm"

    f: Optional[object] = None
    total = 0

    try:
        while True:
            msg = await websocket.receive()

            # Text control messages
            if "text" in msg and msg["text"] is not None:
                try:
                    data = json.loads(msg["text"])
                except Exception:
                    await websocket.send_text(json.dumps({"type": "ERROR", "error": "bad JSON"}))
                    continue

                mtype = data.get("type")
                if mtype == "START":
                    # open file on first START (or reopen if needed)
                    if f is None:
                        f = open(filename, "wb")
                    await websocket.send_text(json.dumps({"type": "ACK", "detail": "recording"}))

                elif mtype == "STOP":
                    if f:
                        f.flush()
                        f.close()
                        f = None
                    await websocket.send_text(json.dumps({
                        "type": "SAVED",
                        "filename": filename,
                        "bytes": total
                    }))
                    await websocket.close()
                    return

                else:
                    await websocket.send_text(json.dumps({"type": "ACK", "detail": f"unknown:{mtype}"}))

                continue

            # Binary audio frames
            if "bytes" in msg and msg["bytes"] is not None:
                if f is None:
                    # If client forgot START, create the file implicitly
                    f = open(filename, "wb")
                chunk = msg["bytes"]
                f.write(chunk)
                total += len(chunk)
                # occasional progress ping (every ~256KB)
                if total % (256 * 1024) < 4096:
                    await websocket.send_text(json.dumps({"type": "ACK", "detail": f"bytes={total}"}))

            # Disconnections
            if msg.get("type") == "websocket.disconnect":
                break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(json.dumps({"type": "ERROR", "error": str(e)}))
        except Exception:
            pass
    finally:
        try:
            if f:
                f.flush()
                f.close()
        except Exception:
            pass
