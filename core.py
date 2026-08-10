"""
Core agent logic shared by the CLI and the web server.
The core works with a provider-neutral message protocol and delegates
provider-specific serialization to the backend adapters.
"""
import json
import logging
import re
import subprocess
import time
from pathlib import Path

import yaml

from llm_backends import BackendError, ConfigurationError, PermissionError, ProviderError, validate_config
from tools import TOOL_FUNCTIONS, TOOL_SCHEMAS, TOOL_PERMISSIONS, resolve_workspace_path

ROOT = Path(__file__).parent
LOGGER = logging.getLogger(__name__)


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if "workspace_root" not in cfg:
        cfg["workspace_root"] = str(ROOT)
    else:
        workspace = Path(cfg["workspace_root"])
        if not workspace.is_absolute():
            cfg["workspace_root"] = str((ROOT / workspace).resolve())
    if "tools" not in cfg:
        cfg["tools"] = {"read": True, "write": True, "shell": False}
    if "agent" not in cfg:
        cfg["agent"] = {
            "max_tool_rounds": 20,
            "max_tool_calls": 50,
            "max_execution_seconds": 300,
        }
    if "shell" not in cfg:
        cfg["shell"] = {"enabled": False, "timeout_seconds": 30, "max_output_chars": 10000}
    return cfg


def load_system_prompt(cfg):
    path = Path(cfg["system_prompt_file"])
    if not path.is_absolute():
        path = ROOT / path
    base = path.read_text(encoding="utf-8")
    memory_path = ROOT / "memory" / "notes.md"
    if memory_path.exists():
        memory = memory_path.read_text(encoding="utf-8")
        base += (
            "\n\n---\n"
            "The following is MM's persistent memory (memory/notes.md), "
            "which is carried over from past sessions. Use it as context - "
            "it reflects real facts about the user and their projects, not assumptions.\n\n"
            + memory
        )
    return base


def _sanitize_session_id(session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", session_id):
        raise ValueError("Invalid session ID")
    return session_id


def load_session(cfg, session_id):
    session_id = _sanitize_session_id(session_id)
    session_dir = ROOT / cfg.get("session_dir", "sessions")
    session_dir.mkdir(exist_ok=True)
    path = session_dir / f"session_{session_id}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Corrupted session file: {path}") from exc
    if not isinstance(data, list):
        raise ValueError("Session data must be a list of messages")
    return data


def save_session(cfg, session_id, messages):
    session_id = _sanitize_session_id(session_id)
    session_dir = ROOT / cfg.get("session_dir", "sessions")
    session_dir.mkdir(exist_ok=True)
    fname = session_dir / f"session_{session_id}.json"
    fname.write_text(json.dumps(messages, indent=2), encoding="utf-8")
    if cfg.get("git_auto_commit", False):
        last_user_msg = next(
            (
                m.get("content")
                for m in reversed(messages)
                if m.get("role") == "user" and isinstance(m.get("content"), str)
            ),
            "session update",
        )
        short = (last_user_msg[:60] + "...") if len(last_user_msg) > 60 else last_user_msg
        if not git_commit_all(f"MM session: {short}"):
            raise RuntimeError("Automatic git commit failed; session was saved but not committed")


def get_web_tool_schemas(cfg):
    if cfg.get("web_enable_shell", False):
        return TOOL_SCHEMAS
    return [t for t in TOOL_SCHEMAS if t["name"] != "run_shell"]


def git_commit_all(message):
    try:
        status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, timeout=15)
    except subprocess.SubprocessError:
        return False

    changed_files = [line[3:].strip() for line in status.stdout.splitlines() if line.strip()]
    denied_patterns = [".env", ".key", "sessions/", "memory/", "tmp_ollama.json", "venv/", ".venv/"]
    allowed_files = []
    for path in changed_files:
        if any(path.startswith(pattern) or pattern in path for pattern in denied_patterns):
            continue
        allowed_files.append(path)

    if not allowed_files:
        return True

    try:
        add_result = subprocess.run(["git", "add", "--"] + allowed_files, cwd=ROOT, capture_output=True, text=True, timeout=15)
        if add_result.returncode != 0:
            LOGGER.error("git add failed: %s", add_result.stderr.strip())
            return False
        result = subprocess.run(["git", "commit", "-m", message], cwd=ROOT, capture_output=True, text=True, timeout=15)
    except subprocess.SubprocessError as exc:
        LOGGER.error("git operation failed: %s", exc)
        return False
    return result.returncode == 0


def _tool_access_allowed(name, cfg):
    permissions = cfg.get("tools", {})
    if name in {"read_file", "list_dir", "search_code"}:
        return permissions.get("read", True)
    if name in {"write_file"}:
        return permissions.get("write", True)
    if name in {"run_shell", "git_commit", "git_push"}:
        return permissions.get("shell", False)
    return True


def _validate_tool_input(name, tool_input, cfg):
    if not isinstance(tool_input, dict):
        raise ValueError(f"Tool '{name}' expects a JSON object argument")

    schema = next((t for t in TOOL_SCHEMAS if t["name"] == name), None)
    if schema is None:
        raise ValueError(f"Unknown tool '{name}'")

    properties = schema.get("input_schema", {}).get("properties", {})
    required = schema.get("input_schema", {}).get("required", [])
    for field in required:
        if field not in tool_input:
            raise ValueError(f"Tool '{name}' missing required field '{field}'")

    for key, value in tool_input.items():
        prop_schema = properties.get(key, {})
        if key in {"path", "source_dir", "exports_dir", "memory_path"} and isinstance(value, str):
            resolve_workspace_path(value, workspace_root=cfg.get("workspace_root", ROOT))
        if key == "command" and not isinstance(value, str):
            raise ValueError("'command' must be a string")
        if key == "timeout" and not isinstance(value, (int, float)):
            raise ValueError("'timeout' must be numeric")
        if key == "content" and not isinstance(value, str):
            raise ValueError("'content' must be a string")
        if prop_schema.get("type") == "string" and not isinstance(value, str):
            raise ValueError(f"Field '{key}' must be a string")


def execute_tool(name, tool_input, cfg=None, confirm_shell=True, allow_shell=False):
    cfg = cfg or {}
    if not _tool_access_allowed(name, cfg):
        raise PermissionError(f"Tool '{name}' is not permitted by configuration")

    _validate_tool_input(name, tool_input, cfg)

    fn = TOOL_FUNCTIONS.get(name)
    if not fn:
        raise ValueError(f"Unknown tool '{name}'")

    if name == "run_shell":
        shell_cfg = cfg.get("shell", {})
        timeout_seconds = shell_cfg.get("timeout_seconds", 30)
        max_output_chars = shell_cfg.get("max_output_chars", 10000)
        cwd = resolve_workspace_path(shell_cfg.get("cwd", "."), cfg.get("workspace_root", ROOT))
        if not shell_cfg.get("enabled", False):
            raise PermissionError("Shell execution is disabled")
        if not allow_shell or not confirm_shell:
            raise PermissionError("Shell execution requires explicit approval")
        return fn(
            tool_input["command"],
            allow_shell=True,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            cwd=cwd,
        )

    kwargs = dict(tool_input)
    kwargs["workspace_root"] = cfg.get("workspace_root", ROOT)
    return fn(**kwargs)


def agent_turn(backend, cfg, messages, system_prompt, tool_schemas, confirm_shell=True, on_tool_call=None):
    """Run a bounded agent turn with tool execution and provider-neutral messages."""
    agent_cfg = cfg.get("agent", {})
    max_tool_rounds = int(agent_cfg.get("max_tool_rounds", 20))
    max_tool_calls = int(agent_cfg.get("max_tool_calls", 50))
    max_execution_seconds = int(agent_cfg.get("max_execution_seconds", 300))

    round_index = 0
    tool_call_count = 0
    start_time = time.monotonic()

    while round_index < max_tool_rounds:
        if time.monotonic() - start_time > max_execution_seconds:
            return f"Agent stopped: execution budget of {max_execution_seconds}s exceeded"

        result = backend.send(messages, tool_schemas, system_prompt)
        if not result.get("tool_calls"):
            messages.append({"role": "assistant", "content": result.get("text", "")})
            return result.get("text", "")

        assistant_content = []
        if result.get("text"):
            assistant_content.append({"type": "text", "text": result["text"]})
        for index, tc in enumerate(result["tool_calls"]):
            tc["id"] = tc.get("id") or f"tool-{round_index}-{index}"
            assistant_content.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]})
        messages.append({"role": "assistant", "content": assistant_content})

        tool_results = []
        for tc in result["tool_calls"]:
            if tool_call_count >= max_tool_calls:
                return f"Agent stopped: tool call budget of {max_tool_calls} exceeded"
            try:
                output = execute_tool(
                    tc["name"],
                    tc["input"],
                    cfg=cfg,
                    confirm_shell=confirm_shell,
                    allow_shell=cfg.get("tools", {}).get("shell", False),
                )
            except PermissionError as exc:
                return f"Permission required: {exc}"
            except (ValueError, FileNotFoundError, PermissionError, TimeoutError, OSError) as exc:
                return f"Tool execution failed: {exc}"
            if on_tool_call:
                on_tool_call(tc["name"], tc["input"], output)
            tool_results.append({"type": "tool_result", "tool_use_id": tc["id"], "content": str(output)})
            tool_call_count += 1

        messages.append({"role": "user", "content": tool_results})
        round_index += 1

    return f"Agent stopped: reached the maximum tool round limit of {max_tool_rounds}"
