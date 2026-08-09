"""
Core agent logic shared by the CLI (agent.py) and the web server (web/server.py).
Keeping this separate means both front-ends stay in sync automatically -
one brain, two faces.
"""
import json
import datetime
import subprocess
from pathlib import Path

import yaml

from llm_backends import get_backend, BackendError
from tools import TOOL_SCHEMAS, TOOL_FUNCTIONS

ROOT = Path(__file__).parent

# Tools considered safe to expose without a human confirming each call.
# run_shell is excluded by default in web contexts - see get_web_tool_schemas().
SAFE_TOOL_NAMES = {"read_file", "write_file", "list_dir", "search_code", "remember", "export_zip"}


def load_config():
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def load_system_prompt(cfg):
    path = ROOT / cfg["system_prompt_file"]
    base = path.read_text()
    memory_path = ROOT / "memory" / "notes.md"
    if memory_path.exists():
        memory = memory_path.read_text()
        base += (
            "\n\n---\n"
            "The following is MM's persistent memory (memory/notes.md), "
            "version-controlled in git and carried over from past sessions. "
            "Use it as context - it reflects real facts about the user and "
            "their projects, not assumptions.\n\n" + memory
        )
    return base


def git_commit_all(message):
    try:
        subprocess.run(["git", "add", "-A"], cwd=ROOT, capture_output=True, timeout=15)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


def save_session(cfg, session_id, messages):
    session_dir = ROOT / cfg["session_dir"]
    session_dir.mkdir(exist_ok=True)
    fname = session_dir / f"session_{session_id}.json"
    fname.write_text(json.dumps(messages, indent=2))
    if cfg.get("git_auto_commit", True):
        last_user_msg = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user" and isinstance(m["content"], str)),
            "session update",
        )
        short = (last_user_msg[:60] + "...") if len(last_user_msg) > 60 else last_user_msg
        git_commit_all(f"MM session: {short}")


def get_web_tool_schemas(cfg):
    """Web UI excludes run_shell by default (no way to prompt for
    confirmation mid-request). Enable via config: web_enable_shell: true."""
    if cfg.get("web_enable_shell", False):
        return TOOL_SCHEMAS
    return [t for t in TOOL_SCHEMAS if t["name"] != "run_shell"]


def execute_tool(name, tool_input, confirm_shell=True):
    fn = TOOL_FUNCTIONS.get(name)
    if not fn:
        return f"ERROR: unknown tool {name}"
    if name == "run_shell":
        return fn(tool_input["command"], confirm=confirm_shell)
    return fn(**tool_input)


def agent_turn(backend, cfg, messages, system_prompt, tool_schemas, confirm_shell=True, on_tool_call=None):
    """Runs one full turn: send to LLM, execute any tool calls, repeat
    until the model responds with plain text and no more tool calls.

    on_tool_call: optional callback(name, input, output) fired after each
    tool executes - lets the web server stream progress to the UI.
    """
    while True:
        result = backend.send(messages, tool_schemas, system_prompt)

        if not result["tool_calls"]:
            messages.append({"role": "assistant", "content": result["text"]})
            return result["text"]

        assistant_content = []
        if result["text"]:
            assistant_content.append({"type": "text", "text": result["text"]})
        for tc in result["tool_calls"]:
            assistant_content.append({
                "type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"],
            })
        messages.append({"role": "assistant", "content": assistant_content})

        tool_results = []
        for tc in result["tool_calls"]:
            output = execute_tool(tc["name"], tc["input"], confirm_shell)
            if on_tool_call:
                on_tool_call(tc["name"], tc["input"], output)
            tool_results.append({
                "type": "tool_result", "tool_use_id": tc["id"], "content": str(output),
            })
        messages.append({"role": "user", "content": tool_results})
