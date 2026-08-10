"""
Provider adapters for Anthropic, OpenAI, and Ollama.
The agent core speaks a provider-neutral message protocol and leaves
provider-specific serialization and parsing to these adapters.
"""
import json
import os
import re
import subprocess

import requests
from requests import exceptions as requests_exceptions


class ApplicationError(RuntimeError):
    pass


class BackendError(ApplicationError):
    pass


class ConfigurationError(ApplicationError, ValueError):
    pass


class ProviderError(BackendError):
    pass


class PermissionError(ApplicationError):
    pass


class InvalidModelResponseError(ApplicationError):
    pass


def _parse_tool_arguments(arguments, provider):
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str):
        raise InvalidModelResponseError(f"{provider} returned non-object tool arguments")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise InvalidModelResponseError(f"{provider} returned malformed tool arguments") from exc
    if not isinstance(parsed, dict):
        raise InvalidModelResponseError(f"{provider} tool arguments must be a JSON object")
    return parsed


def _extract_text_tool_calls(content, provider):
    """Recover tool calls emitted as JSON text by models without native support."""
    if not isinstance(content, str):
        return content or "", []

    from tools import TOOL_FUNCTIONS

    candidates = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", content):
        try:
            _, end = decoder.raw_decode(content[match.start():])
        except json.JSONDecodeError:
            continue
        candidates.append((match.start(), content[match.start():match.start() + end]))
    for start, candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict) or parsed.get("name") not in TOOL_FUNCTIONS:
            continue
        arguments = parsed.get("arguments", parsed.get("input", {}))
        tool_input = _parse_tool_arguments(arguments, provider)
        visible_text = (content[:start] + content[start + len(candidate):]).strip()
        visible_text = re.sub(r"```(?:json)?", "", visible_text, flags=re.IGNORECASE).replace("```", "").strip()
        return visible_text, [{"name": parsed["name"], "input": tool_input, "id": f"{provider.lower()}-text-0"}]
    return content, []


def _post_json(url, **kwargs):
    try:
        return requests.post(url, **kwargs)
    except requests_exceptions.RequestException as exc:
        raise ProviderError(f"Provider request failed: {exc}") from exc


def validate_config(cfg: dict):
    active_backend = cfg.get("active_backend")
    if not active_backend:
        raise ConfigurationError("No active backend configured")
    backends = cfg.get("backends", {})
    if active_backend not in backends:
        raise ConfigurationError(f"Unknown backend '{active_backend}'")

    backend_cfg = backends[active_backend]
    if active_backend in {"anthropic", "openai"}:
        key_env = backend_cfg.get("api_key_env")
        if not key_env or not os.environ.get(key_env):
            raise ConfigurationError(f"{active_backend.capitalize()} backend selected but {key_env or 'API key env var'} is missing")

    if active_backend == "ollama_local" and not backend_cfg.get("base_url"):
        raise ConfigurationError("Ollama backend requires a base_url")

    workspace_root = cfg.get("workspace_root")
    if not workspace_root:
        raise ConfigurationError("workspace_root must be configured")
    if not os.path.isdir(workspace_root):
        raise ConfigurationError(f"workspace_root does not exist: {workspace_root}")

    shell_cfg = cfg.get("shell", {})
    if not isinstance(shell_cfg.get("timeout_seconds", 30), (int, float)):
        raise ConfigurationError("shell.timeout_seconds must be numeric")
    if not isinstance(shell_cfg.get("max_output_chars", 10000), int):
        raise ConfigurationError("shell.max_output_chars must be an integer")
    web_host = cfg.get("web_host", "127.0.0.1")
    if web_host not in {"127.0.0.1", "localhost", "::1"}:
        auth_env = cfg.get("web_auth_token_env", "MM_WEB_AUTH_TOKEN")
        if not os.environ.get(auth_env):
            raise ConfigurationError("Non-local web_host requires a web authentication token")

    agent_cfg = cfg.get("agent", {})
    for key in ("max_tool_rounds", "max_tool_calls", "max_execution_seconds"):
        value = agent_cfg.get(key)
        if value is not None and not isinstance(value, int):
            raise ConfigurationError(f"agent.{key} must be an integer")

    return True


def get_backend(cfg: dict):
    validate_config(cfg)
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
        self.api_key = os.environ.get(key_env) or cfg.get("api_key")

    def serialize_tools(self, tools):
        return [{"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("input_schema", {})} for t in tools or []]

    def convert_messages(self, messages):
        converted = []
        for message in messages or []:
            role = message.get("role", "user")
            if role == "tool":
                role = "user"
            content = self._to_anthropic_content(message.get("content", ""))
            converted.append({"role": role, "content": content})
        return converted

    def _to_anthropic_content(self, content):
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content)
        blocks = []
        for item in content:
            item_type = item.get("type")
            if item_type == "text":
                blocks.append({"type": "text", "text": item.get("text", "")})
            elif item_type == "tool_use":
                blocks.append({"type": "tool_use", "id": item.get("id"), "name": item.get("name"), "input": item.get("input", {})})
            elif item_type == "tool_result":
                blocks.append({"type": "tool_result", "tool_use_id": item.get("tool_use_id"), "content": item.get("content", "")})
            else:
                blocks.append({"type": "text", "text": str(item)})
        if len(blocks) == 1 and blocks[0]["type"] == "text":
            return blocks[0]["text"]
        return blocks

    def parse_response(self, data):
        text_parts = []
        tool_calls = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append({"name": block["name"], "input": block.get("input", {}), "id": block.get("id")})
        return {"text": "\n".join(text_parts), "tool_calls": tool_calls, "raw": data}

    def send(self, messages, tools, system_prompt):
        if not self.api_key:
            raise BackendError("Missing API key")
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self.cfg["model"],
            "max_tokens": self.cfg.get("max_tokens", 4096),
            "system": system_prompt,
            "messages": self.convert_messages(messages),
        }
        if tools:
            payload["tools"] = self.serialize_tools(tools)
        resp = _post_json(self.cfg["base_url"], headers=headers, json=payload, timeout=120)
        if resp.status_code != 200:
            raise ProviderError(f"Anthropic API error {resp.status_code}: {resp.text}")
        data = resp.json()
        return self.parse_response(data)


class OpenAIBackend:
    def __init__(self, cfg):
        self.cfg = cfg
        key_env = cfg.get("api_key_env", "OPENAI_API_KEY")
        self.api_key = os.environ.get(key_env) or cfg.get("api_key")

    def serialize_tools(self, tools):
        serialized = []
        for tool in tools or []:
            serialized.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {}),
                    },
                }
            )
        return serialized

    def convert_messages(self, messages):
        converted = []
        for message in messages or []:
            role = message.get("role", "user")
            content = message.get("content", "")
            if role == "system":
                converted.append({"role": "system", "content": self._to_text_content(content)})
                continue
            if role == "assistant":
                blocks = self._to_blocks(content)
                text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
                tool_calls = []
                for block in blocks:
                    if block.get("type") == "tool_use":
                        tool_calls.append(
                            {
                                "id": block.get("id"),
                                "type": "function",
                                "function": {
                                    "name": block.get("name"),
                                    "arguments": json.dumps(block.get("input", {})),
                                },
                            }
                        )
                assistant_message = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else ""}
                if tool_calls:
                    assistant_message["tool_calls"] = tool_calls
                converted.append(assistant_message)
                continue
            if role == "tool":
                converted.append({"role": "tool", "tool_call_id": message.get("tool_call_id"), "content": self._to_text_content(content)})
                continue
            blocks = self._to_blocks(content)
            text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            if text_parts:
                converted.append({"role": "user", "content": "\n".join(text_parts)})
            for block in blocks:
                if block.get("type") == "tool_result":
                    converted.append({"role": "tool", "tool_call_id": block.get("tool_use_id"), "content": str(block.get("content", ""))})
        return converted

    def _to_blocks(self, content):
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return [{"type": "text", "text": str(content)}]
        return [
            {"type": item.get("type", "text"), "text": item.get("text", ""), "tool_use_id": item.get("tool_use_id"), "id": item.get("id"), "name": item.get("name"), "input": item.get("input", {}), "content": item.get("content", "")}
            for item in content
        ]

    def _to_text_content(self, content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "tool_result":
                    parts.append(str(item.get("content", "")))
            return "\n".join(parts)
        return str(content)

    def parse_response(self, data):
        msg = data["choices"][0]["message"]
        tool_calls = []
        for tc in msg.get("tool_calls", []) or []:
            args = tc.get("function", {}).get("arguments", "")
            parsed_input = _parse_tool_arguments(args, "OpenAI")
            tool_calls.append({"name": tc["function"]["name"], "input": parsed_input, "id": tc.get("id") or tc["function"]["name"]})
        return {"text": msg.get("content") or "", "tool_calls": tool_calls, "raw": data}

    def send(self, messages, tools, system_prompt):
        if not self.api_key:
            raise BackendError("Missing API key")
        headers = {"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"}
        full_messages = [{"role": "system", "content": system_prompt}] + self.convert_messages(messages)
        payload = {
            "model": self.cfg["model"],
            "max_tokens": self.cfg.get("max_tokens", 4096),
            "messages": full_messages,
        }
        if tools:
            payload["tools"] = self.serialize_tools(tools)
        resp = _post_json(self.cfg["base_url"], headers=headers, json=payload, timeout=120)
        if resp.status_code != 200:
            raise ProviderError(f"OpenAI API error {resp.status_code}: {resp.text}")
        data = resp.json()
        return self.parse_response(data)


class OllamaBackend:
    def __init__(self, cfg):
        self.cfg = cfg

    def serialize_tools(self, tools):
        return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("input_schema", {})}} for t in tools or []]

    def convert_messages(self, messages):
        return OpenAIBackend({"model": self.cfg.get("model"), "base_url": self.cfg.get("base_url")}).convert_messages(messages)

    def parse_response(self, data):
        msg = data.get("message", {})
        tool_calls = []
        for tc in msg.get("tool_calls", []) or []:
            args = tc.get("function", {}).get("arguments", "")
            name = tc.get("function", {}).get("name")
            if not name:
                raise InvalidModelResponseError("Ollama returned a tool call without a name")
            parsed_input = _parse_tool_arguments(args, "Ollama")
            tool_calls.append({"name": name, "input": parsed_input, "id": tc.get("id") or name})
        text, text_tool_calls = _extract_text_tool_calls(msg.get("content", ""), "Ollama")
        return {"text": text, "tool_calls": tool_calls or text_tool_calls, "raw": data}

    def _send_via_http(self, messages, tools, system_prompt):
        full_messages = [{"role": "system", "content": system_prompt}] + self.convert_messages(messages)
        payload = {"model": self.cfg["model"], "messages": full_messages, "stream": False}
        if tools:
            payload["tools"] = self.serialize_tools(tools)
        resp = _post_json(
            self.cfg["base_url"],
            json=payload,
            timeout=self.cfg.get("request_timeout_seconds", 180),
        )
        if resp.status_code != 200:
            raise ProviderError(f"Ollama error {resp.status_code}: {resp.text}")
        data = resp.json()
        return self.parse_response(data)

    def _send_via_cli(self, messages, tools, system_prompt):
        prompt_lines = [system_prompt.strip(), ""]
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            if isinstance(content, list):
                text_parts = [item.get("text", "") for item in content if item.get("type") == "text"]
                content = "\n".join(text_parts)
            if role == "user":
                prompt_lines.append(f"User: {content}")
            elif role == "assistant":
                prompt_lines.append(f"Assistant: {content}")
            else:
                prompt_lines.append(str(content))
        if tools:
            prompt_lines.extend([
                "Assistant: Return only JSON in this shape:",
                '{"text":"optional reply","tool_calls":[{"name":"tool_name","arguments":{}}]}',
            ])
        else:
            prompt_lines.append("Assistant:")
        prompt = "\n".join(prompt_lines)
        try:
            result = subprocess.run(["ollama", "run", self.cfg["model"], prompt], capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"Ollama CLI timeout: {exc}") from exc
        if result.returncode != 0:
            raise ProviderError(f"Ollama CLI failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
        output = result.stdout.strip()
        if not tools:
            return {"text": output, "tool_calls": [], "raw": result.stdout}
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return {"text": output, "tool_calls": [], "raw": result.stdout}
        tool_calls = []
        for index, call in enumerate(parsed.get("tool_calls", []) or []):
            if not isinstance(call, dict) or not call.get("name"):
                raise InvalidModelResponseError("Ollama CLI returned an invalid tool call")
            tool_calls.append({
                "name": call["name"],
                "input": _parse_tool_arguments(call.get("arguments", call.get("input", {})), "Ollama CLI"),
                "id": call.get("id") or f"ollama-cli-{index}",
            })
        return {"text": parsed.get("text", ""), "tool_calls": tool_calls, "raw": result.stdout}

    def send(self, messages, tools, system_prompt):
        try:
            return self._send_via_http(messages, tools, system_prompt)
        except requests_exceptions.RequestException as exc:
            if not self.cfg.get("allow_cli_fallback", False):
                raise ProviderError(f"Ollama HTTP transport failed and CLI fallback is disabled: {exc}") from exc
            return self._send_via_cli(messages, tools, system_prompt)
        except ProviderError as exc:
            if self.cfg.get("allow_cli_fallback", False) and not tools:
                return self._send_via_cli(messages, tools, system_prompt)
            raise
