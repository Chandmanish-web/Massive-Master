#!/usr/bin/env python3
"""
MM (massive-master) CLI - your private agent for building apps/web apps,
coding help, and general chat/problem-solving.

Usage:
    python agent.py                 # interactive chat loop
    python agent.py "build a flask hello world app"   # one-shot prompt

Also see: web/server.py for the browser chat UI version of MM.

Setup:
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-ant-...     # (or OPENAI_API_KEY, or use ollama_local)
    edit config.yaml to pick your backend
    edit prompts/system_prompt.md to define MM's purpose/behavior
"""
import datetime
import sys

from llm_backends import BackendError, ConfigurationError, get_backend
from tools import TOOL_SCHEMAS
from core import agent_turn, load_config, load_system_prompt, save_session


def main():
    cfg = load_config()
    system_prompt = load_system_prompt(cfg)

    try:
        backend = get_backend(cfg)
    except (ConfigurationError, BackendError) as exc:
        print(f"Backend setup error: {exc}")
        sys.exit(1)

    messages = []
    session_id = datetime.date.today().isoformat()
    confirm_shell = cfg.get("confirm_shell_commands", True)
    print(f"MM (massive-master) ready — backend: {cfg['active_backend']}. Type 'exit' to quit.\n")

    def on_tool_call(name, tool_input, output):
        print(f"  -> {name}({tool_input})")

    def run_turn(prompt):
        messages.append({"role": "user", "content": prompt})
        text = agent_turn(
            backend,
            cfg,
            messages,
            system_prompt,
            TOOL_SCHEMAS,
            confirm_shell=confirm_shell,
            on_tool_call=on_tool_call,
        )
        if text:
            print(f"\n{text}\n")
        save_session(cfg, session_id, messages)

    if len(sys.argv) > 1:
        run_turn(" ".join(sys.argv[1:]))
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
        try:
            run_turn(user_input)
        except BackendError as exc:
            print(f"Error: {exc}")


if __name__ == "__main__":
    main()
