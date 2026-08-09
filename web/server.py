#!/usr/bin/env python3
"""
MM web server - serves the browser chat UI and handles chat requests.

Run from the project root:
    python -m web.server

Then open http://localhost:8000
"""
import sys
import uuid
import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))
from llm_backends import get_backend, BackendError
from core import load_config, load_system_prompt, save_session, agent_turn, get_web_tool_schemas

ROOT = Path(__file__).parent.parent
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="MM - massive-master")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# In-memory session store: {session_id: [messages]}
# Simple by design - full history is also persisted to sessions/*.json + git.
SESSIONS = {}

cfg = load_config()
system_prompt = load_system_prompt(cfg)
tool_schemas = get_web_tool_schemas(cfg)

try:
    backend = get_backend(cfg)
except BackendError as e:
    print(f"[MM] Backend setup error: {e}")
    print("[MM] Set your API key or switch to ollama_local in config.yaml, then restart.")
    backend = None


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    tool_calls: list


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {
        "status": "ok" if backend else "backend_error",
        "backend": cfg["active_backend"],
    }


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if backend is None:
        raise HTTPException(500, "LLM backend not configured - check your API key / config.yaml")

    session_id = req.session_id or str(uuid.uuid4())[:8]
    messages = SESSIONS.setdefault(session_id, [])

    tool_log = []

    def on_tool_call(name, tool_input, output):
        tool_log.append({"name": name, "input": tool_input, "output": str(output)[:500]})

    messages.append({"role": "user", "content": req.message})
    try:
        reply = agent_turn(
            backend, cfg, messages, system_prompt, tool_schemas,
            confirm_shell=False,  # web UI never runs shell without explicit config opt-in
            on_tool_call=on_tool_call,
        )
    except BackendError as e:
        raise HTTPException(500, str(e))

    save_session(cfg, f"web_{session_id}_{datetime.date.today()}", messages)
    return ChatResponse(session_id=session_id, reply=reply or "", tool_calls=tool_log)


@app.post("/api/new_session")
def new_session():
    session_id = str(uuid.uuid4())[:8]
    SESSIONS[session_id] = []
    return {"session_id": session_id}


if __name__ == "__main__":
    import uvicorn
    print(f"MM web UI starting — backend: {cfg['active_backend']}")
    print("Open http://localhost:8000 in your browser")
    uvicorn.run(app, host="127.0.0.1", port=8000)
