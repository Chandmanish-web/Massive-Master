#!/usr/bin/env python3
"""
MM web server - serves the browser chat UI and handles chat requests.
"""
import datetime
import hmac
import os
import sys
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))
from core import agent_turn, get_web_tool_schemas, load_config, load_session, load_system_prompt, save_session
from llm_backends import BackendError, ConfigurationError, get_backend

ROOT = Path(__file__).parent.parent
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="MM - massive-master")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

SESSION_LOCK = threading.Lock()

cfg = load_config()
system_prompt = load_system_prompt(cfg)
tool_schemas = get_web_tool_schemas(cfg)

try:
    backend = get_backend(cfg)
except (ConfigurationError, BackendError) as exc:
    print(f"[MM] Backend setup error: {exc}")
    print("[MM] Set your API key or switch to ollama_local in config.yaml, then restart.")
    backend = None
BACKENDS = {cfg["active_backend"]: backend} if backend else {}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    backend: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    tool_calls: list


def _require_auth(token: str | None):
    host = cfg.get("web_host", "127.0.0.1")
    if host in {"127.0.0.1", "localhost", "::1"}:
        return
    env_name = cfg.get("web_auth_token_env", "MM_WEB_AUTH_TOKEN")
    expected = os.environ.get(env_name)
    if not expected:
        raise HTTPException(503, "Web authentication is not configured")
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(401, "Invalid or missing authentication token")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health(x_mm_auth: str | None = Header(default=None)):
    _require_auth(x_mm_auth)
    return {
        "status": "ok" if backend else "backend_error",
        "backend": cfg["active_backend"],
        "backends": list(cfg.get("backends", {})),
    }


@app.get("/api/backends")
def list_backends(x_mm_auth: str | None = Header(default=None)):
    _require_auth(x_mm_auth)
    result = []
    for name, backend_cfg in cfg.get("backends", {}).items():
        key_env = backend_cfg.get("api_key_env")
        configured = not key_env or bool(os.environ.get(key_env) or backend_cfg.get("api_key"))
        result.append({"name": name, "active": name == cfg["active_backend"], "configured": configured})
    return {"backends": result}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest, x_mm_auth: str | None = Header(default=None)):
    _require_auth(x_mm_auth)
    backend_name = req.backend or cfg["active_backend"]
    if backend_name not in cfg.get("backends", {}):
        raise HTTPException(400, f"Unknown backend: {backend_name}")
    selected_backend = BACKENDS.get(backend_name)
    if selected_backend is None:
        selected_cfg = dict(cfg)
        selected_cfg["active_backend"] = backend_name
        try:
            selected_backend = get_backend(selected_cfg)
        except (ConfigurationError, BackendError) as exc:
            raise HTTPException(503, str(exc)) from exc
        BACKENDS[backend_name] = selected_backend

    session_id = req.session_id or str(uuid.uuid4())[:8]
    try:
        with SESSION_LOCK:
            messages = load_session(cfg, f"web_{session_id}_{datetime.date.today().isoformat()}")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    tool_log = []

    def on_tool_call(name, tool_input, output):
        tool_log.append({"name": name, "input": tool_input, "output": str(output)[:500]})

    messages.append({"role": "user", "content": req.message})
    try:
        reply = agent_turn(
            selected_backend,
            cfg,
            messages,
            system_prompt,
            tool_schemas,
            confirm_shell=False,
            on_tool_call=on_tool_call,
        )
    except BackendError as exc:
        raise HTTPException(500, str(exc))

    with SESSION_LOCK:
        save_session(cfg, f"web_{session_id}_{datetime.date.today().isoformat()}", messages)
    return ChatResponse(session_id=session_id, reply=reply or "", tool_calls=tool_log)


@app.post("/api/new_session")
def new_session(x_mm_auth: str | None = Header(default=None)):
    _require_auth(x_mm_auth)
    session_id = str(uuid.uuid4())[:8]
    return {"session_id": session_id}


if __name__ == "__main__":
    import uvicorn
    print(f"MM web UI starting — backend: {cfg['active_backend']}")
    print("Open http://localhost:8000 in your browser")
    uvicorn.run(app, host=cfg.get("web_host", "127.0.0.1"), port=cfg.get("web_port", 8000))
