"""
Unified interface over multiple LLM providers so the rest of the agent
never has to know which backend is active.

Each backend exposes: send(messages, tools) -> dict with keys:
    - "text": assistant's plain text (may be empty)
    - "tool_calls": list of {"name": str, "input": dict, "id": str}
    - "raw": provider's raw response (for debugging)
"""
import os
import json
import subprocess
import requests


class BackendError(RuntimeError):
    pass


def get_backend(cfg: dict):
    name = cfg["active_backend"]
    backend_cfg = cfg["backends"][name]
    if name == "anthropic":
        return AnthropicBackend(backend_cfg)
    if name == "openai":
        return OpenAIBackend(backend_cfg)
    if name == "ollama_local":
        return OllamaBackend(backend_cfg)
    raise BackendError(f"Unknown backend: {name}")


class AnthropicBackend:
    def __init__(self, cfg):
        self.cfg = cfg
        key_env = cfg.get("api_key_env", "ANTHROPIC_API_KEY")
        self.api_key = os.environ.get(key_env)
        if not self.api_key:
            raise BackendError(
                f"Missing API key. Run: export {key_env}=sk-ant-..."
            )

    def send(self, messages, tools, system_prompt):
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self.cfg["model"],
            "max_tokens": self.cfg.get("max_tokens", 4096),
            "system": system_prompt,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        resp = requests.post(self.cfg["base_url"], headers=headers, json=payload, timeout=120)
        if resp.status_code != 200:
            raise BackendError(f"Anthropic API error {resp.status_code}: {resp.text}")
        data = resp.json()
        text_parts, tool_calls = [], []
        for block in data.get("content", []):
            if block["type"] == "text":
                text_parts.append(block["text"])
            elif block["type"] == "tool_use":
                tool_calls.append({"name": block["name"], "input": block["input"], "id": block["id"]})
        return {"text": "\n".join(text_parts), "tool_calls": tool_calls, "raw": data}


class OpenAIBackend:
    def __init__(self, cfg):
        self.cfg = cfg
        key_env = cfg.get("api_key_env", "OPENAI_API_KEY")
        self.api_key = os.environ.get(key_env)
        if not self.api_key:
            raise BackendError(f"Missing API key. Run: export {key_env}=sk-...")

    def send(self, messages, tools, system_prompt):
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        payload = {
            "model": self.cfg["model"],
            "max_tokens": self.cfg.get("max_tokens", 4096),
            "messages": full_messages,
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
        resp = requests.post(self.cfg["base_url"], headers=headers, json=payload, timeout=120)
        if resp.status_code != 200:
            raise BackendError(f"OpenAI API error {resp.status_code}: {resp.text}")
        data = resp.json()
        msg = data["choices"][0]["message"]
        tool_calls = []
        for tc in msg.get("tool_calls", []) or []:
            tool_calls.append({
                "name": tc["function"]["name"],
                "input": json.loads(tc["function"]["arguments"]),
                "id": tc["id"],
            })
        return {"text": msg.get("content") or "", "tool_calls": tool_calls, "raw": data}


class OllamaBackend:
    """Fully local backend - no API key, nothing leaves the laptop."""

    def __init__(self, cfg):
        self.cfg = cfg

    def _flatten_message(self, msg):
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "tool_use":
                    parts.append(f"[tool call {item.get('name')} input={item.get('input')}]")
                elif item.get("type") == "tool_result":
                    parts.append(f"[tool result {item.get('tool_use_id')}: {item.get('content')}]")
                else:
                    parts.append(str(item))
            return "\n".join(parts)
        return str(content)

    def _build_prompt(self, messages, system_prompt):
        prompt_lines = [system_prompt.strip(), ""]
        for msg in messages:
            role = msg.get("role", "user")
            text = self._flatten_message(msg).strip()
            if not text:
                continue
            if role == "user":
                prompt_lines.append(f"User: {text}")
            elif role == "assistant":
                prompt_lines.append(f"Assistant: {text}")
            elif role == "system":
                prompt_lines.append(text)
            else:
                prompt_lines.append(f"{role}: {text}")
        prompt_lines.append("Assistant:")
        return "\n".join(prompt_lines)

    def _send_via_cli(self, messages, system_prompt):
        prompt = self._build_prompt(messages, system_prompt)
        try:
            result = subprocess.run(
                ["ollama", "run", self.cfg["model"], prompt],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as e:
            raise BackendError(f"Ollama CLI timeout: {e}")

        if result.returncode != 0:
            raise BackendError(
                f"Ollama CLI failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
            )

        return {"text": result.stdout.strip(), "tool_calls": [], "raw": result.stdout}

    def _send_via_http(self, messages, tools, system_prompt):
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        payload = {
            "model": self.cfg["model"],
            "messages": full_messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
        resp = requests.post(self.cfg["base_url"], json=payload, timeout=30)
        if resp.status_code != 200:
            raise BackendError(f"Ollama error {resp.status_code}: {resp.text}")
        data = resp.json()
        msg = data.get("message", {})
        tool_calls = []
        for tc in msg.get("tool_calls", []) or []:
            tool_calls.append({
                "name": tc["function"]["name"],
                "input": tc["function"]["arguments"],
                "id": tc.get("id", tc["function"]["name"]),
            })
        return {"text": msg.get("content", ""), "tool_calls": tool_calls, "raw": data}

    def send(self, messages, tools, system_prompt):
        try:
            return self._send_via_http(messages, tools, system_prompt)
        except Exception:
            return self._send_via_cli(messages, system_prompt)
