#!/usr/bin/env python3
"""
MM (massive-master) - your private agent for building apps/web apps,
coding help, and general chat/problem-solving.

Usage:
    python agent.py                 # interactive chat loop
    python agent.py "build a flask hello world app"   # one-shot prompt

Setup:
    pip install requests pyyaml
    export ANTHROPIC_API_KEY=sk-ant-...     # (or OPENAI_API_KEY, or use ollama_local)
    edit config.yaml to pick your backend
    edit prompts/system_prompt.md to define MM's purpose/behavior
"""
import sys
import json
import yaml
import datetime
from pathlib import Path

from llm_backends import get_backend, BackendError
from tools import TOOL_SCHEMAS, TOOL_FUNCTIONS, run_shell

HERE = Path(__file__).parent


def load_config():
    with open(HERE / "config.yaml") as f:
        return yaml.safe_load(f)


def load_system_prompt(cfg):
    path = HERE / cfg["system_prompt_file"]
    base = path.read_text()
    memory_path = HERE / "memory" / "notes.md"
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


def save_session(cfg, messages):
    session_dir = HERE / cfg["session_dir"]
    session_dir.mkdir(exist_ok=True)
    fname = session_dir / f"session_{datetime.date.today()}.json"
    fname.write_text(json.dumps(messages, indent=2))
    if cfg.get("git_auto_commit", True):
        last_user_msg = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user" and isinstance(m["content"], str)),
            "session update",
        )
        short = (last_user_msg[:60] + "...") if len(last_user_msg) > 60 else last_user_msg
        git_commit_all(f"MM session: {short}")


def execute_tool(name, tool_input, confirm_shell):
    fn = TOOL_FUNCTIONS.get(name)
    if not fn:
        return f"ERROR: unknown tool {name}"
    if name == "run_shell":
        return fn(tool_input["command"], confirm=confirm_shell)
    return fn(**tool_input)


def git_commit_all(message):
    """Commit any changes (sessions, memory, code) so git IS the history."""
    import subprocess
    try:
        subprocess.run(["git", "add", "-A"], cwd=HERE, capture_output=True, timeout=15)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=HERE, capture_output=True, text=True, timeout=15,
        )
        # non-zero exit if nothing to commit - that's fine, not an error
        return result.returncode == 0
    except Exception:
        return False


def agent_turn(backend, cfg, messages, system_prompt):
    """Runs one full turn: send to LLM, execute any tool calls, repeat
    until the model responds with plain text and no more tool calls."""
    confirm_shell = cfg.get("confirm_shell_commands", True)

    while True:
        result = backend.send(messages, TOOL_SCHEMAS, system_prompt)

        if result["text"]:
            print(f"\n{result['text']}\n")

        if not result["tool_calls"]:
            # Model is done - append its final text and return
            messages.append({"role": "assistant", "content": result["text"]})
            return

        # Append assistant's tool-call turn, then execute each tool
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
            print(f"  -> {tc['name']}({tc['input']})")
            output = execute_tool(tc["name"], tc["input"], confirm_shell)
            tool_results.append({
                "type": "tool_result", "tool_use_id": tc["id"], "content": str(output),
            })
        messages.append({"role": "user", "content": tool_results})
        # loop again so the model can see tool results and continue/finish


def main():
    cfg = load_config()
    system_prompt = load_system_prompt(cfg)

    try:
        backend = get_backend(cfg)
    except BackendError as e:
        print(f"Backend setup error: {e}")
        sys.exit(1)

    messages = []
    print(f"MM (massive-master) ready — backend: {cfg['active_backend']}. Type 'exit' to quit.\n")

    if len(sys.argv) > 1:
        # one-shot mode
        prompt = " ".join(sys.argv[1:])
        messages.append({"role": "user", "content": prompt})
        agent_turn(backend, cfg, messages, system_prompt)
        save_session(cfg, messages)
        return

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break
        if user_input.lower() in ("exit", "quit"):
            break
        if not user_input:
            continue
        messages.append({"role": "user", "content": user_input})
        try:
            agent_turn(backend, cfg, messages, system_prompt)
        except BackendError as e:
            print(f"Error: {e}")
        save_session(cfg, messages)


if __name__ == "__main__":
    main()
