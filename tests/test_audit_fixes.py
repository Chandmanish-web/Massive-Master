import os
import tempfile
import unittest
from unittest.mock import patch
import requests
from pathlib import Path

from core import agent_turn, validate_config
from llm_backends import AnthropicBackend, OllamaBackend, OpenAIBackend, ProviderError
from tools import TOOL_SCHEMAS, export_zip, list_dir, read_file, remember, run_shell, scaffold_project, search_code, write_file


class ProviderAdapterTests(unittest.TestCase):
    def test_openai_tool_schema_conversion(self):
        backend = OpenAIBackend({"model": "gpt-4.1", "base_url": "https://example.test"})
        tools = backend.serialize_tools(TOOL_SCHEMAS)
        self.assertTrue(tools)
        self.assertEqual(tools[0]["type"], "function")
        self.assertIn("parameters", tools[0]["function"])
        self.assertEqual(tools[0]["function"]["name"], "read_file")

    def test_openai_tool_call_parsing(self):
        backend = OpenAIBackend({"model": "gpt-4.1", "base_url": "https://example.test"})
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "done",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "README.md"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
        parsed = backend.parse_response(payload)
        self.assertEqual(parsed["text"], "done")
        self.assertEqual(parsed["tool_calls"][0]["name"], "read_file")
        self.assertEqual(parsed["tool_calls"][0]["input"], {"path": "README.md"})

    def test_tool_result_conversion(self):
        backend = OpenAIBackend({"model": "gpt-4.1", "base_url": "https://example.test"})
        messages = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "README.md"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}]},
        ]
        converted = backend.convert_messages(messages)
        self.assertTrue(any(message.get("role") == "tool" for message in converted))

    def test_openai_uses_configured_api_key(self):
        backend = OpenAIBackend({"api_key": "test-key", "model": "gpt-4.1", "base_url": "https://example.test"})
        with patch("llm_backends.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
            backend.send([], [], "system")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_ollama_recovers_text_tool_call(self):
        backend = OllamaBackend({"model": "qwen2.5-coder:1.5b", "base_url": "http://example.test"})
        result = backend.parse_response({
            "message": {
                "content": '```json\n{"name":"remember","arguments":{"section":"Projects","note":"test"}}\n```'
            }
        })
        self.assertEqual(result["text"], "")
        self.assertEqual(result["tool_calls"][0]["name"], "remember")
        self.assertEqual(result["tool_calls"][0]["input"]["note"], "test")

    def test_ollama_ignores_unknown_text_tool(self):
        backend = OllamaBackend({"model": "qwen2.5-coder:1.5b", "base_url": "http://example.test"})
        result = backend.parse_response({
            "message": {"content": '{"name":"hello","arguments":{}}'}
        })
        self.assertEqual(result["tool_calls"], [])
        self.assertIn('"name":"hello"', result["text"])

    def test_failed_request_is_reported(self):
        backend = OpenAIBackend({"api_key": "test-key", "model": "gpt-4.1", "base_url": "https://example.test"})
        with patch("llm_backends.requests.post", side_effect=requests.exceptions.ConnectionError("network")):
            with self.assertRaises(ProviderError):
                backend.send([], [], "system")


class FilesystemSandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True)
        (self.workspace / "hello.txt").write_text("hello world", encoding="utf-8")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_valid_workspace_path(self):
        resolved = write_file("hello.txt", "x", workspace_root=str(self.workspace))
        self.assertTrue((self.workspace / "hello.txt").exists())
        self.assertEqual(resolved, "Wrote 1 bytes")

    def test_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            read_file("../outside.txt", workspace_root=str(self.workspace))

    def test_absolute_path_outside_workspace_is_rejected(self):
        with self.assertRaises(ValueError):
            read_file(str(self.root / "outside.txt"), workspace_root=str(self.workspace))

    def test_symlink_escape_is_rejected(self):
        target = self.root / "outside.txt"
        target.write_text("secret", encoding="utf-8")
        link = self.workspace / "link"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlink creation is not available in this environment")
        with self.assertRaises(ValueError):
            read_file(str(link), workspace_root=str(self.workspace))

    def test_oversized_file_is_truncated(self):
        path = self.workspace / "big.txt"
        path.write_text("x" * 3000, encoding="utf-8")
        content = read_file(str(path), workspace_root=str(self.workspace), max_bytes=100)
        self.assertIn("[truncated", content)

    def test_memory_and_export_are_sandboxed(self):
        remember("Projects", "test", workspace_root=str(self.workspace))
        self.assertTrue((self.workspace / "memory" / "notes.md").exists())
        (self.workspace / "project").mkdir()
        (self.workspace / "project" / "a.txt").write_text("a", encoding="utf-8")
        result = export_zip("project", "project", workspace_root=str(self.workspace))
        self.assertIn("Exported 1 files", result)
        self.assertIn("ERROR", export_zip("project", "../escape", workspace_root=str(self.workspace)))

    def test_export_omits_runtime_secrets(self):
        project = self.workspace / "project"
        project.mkdir()
        (project / ".env").write_text("SECRET=value", encoding="utf-8")
        (project / ".env.example").write_text("SECRET=", encoding="utf-8")
        export_zip("project", "safe-project", workspace_root=str(self.workspace))
        import zipfile
        with zipfile.ZipFile(self.workspace / "exports" / "safe-project.zip") as archive:
            self.assertNotIn("project/.env", archive.namelist())
            self.assertIn("project/.env.example", archive.namelist())

    def test_scaffold_project_creates_framework_structure(self):
        result = scaffold_project("demo-app", "mern", workspace_root=str(self.workspace))
        self.assertIn("Scaffolded mern project", result)
        self.assertTrue((self.workspace / "projects" / "demo-app" / "client" / "src" / "main.jsx").exists())
        self.assertTrue((self.workspace / "projects" / "demo-app" / "server" / "src" / "index.js").exists())
        self.assertIn("already exists", scaffold_project("demo-app", "mern", workspace_root=str(self.workspace)))


class ShellSecurityTests(unittest.TestCase):
    def test_disabled_shell_is_rejected(self):
        with self.assertRaises(PermissionError):
            run_shell("echo hi", allow_shell=False)

    def test_approved_shell_runs(self):
        result = run_shell("echo hi", allow_shell=True, timeout_seconds=5, max_output_chars=500)
        self.assertIn("hi", result)

    def test_timeout_is_reported(self):
        with self.assertRaises(TimeoutError):
            run_shell("python -c \"import time; time.sleep(10)\"", allow_shell=True, timeout_seconds=1)


class AgentLoopTests(unittest.TestCase):
    def test_agent_stops_after_max_tool_rounds(self):
        class FakeBackend:
            def __init__(self):
                self.calls = 0

            def send(self, messages, tools, system_prompt):
                self.calls += 1
                return {
                    "text": "",
                    "tool_calls": [{"name": "read_file", "input": {"path": "README.md"}, "id": "call_1"}],
                    "raw": {},
                }

        backend = FakeBackend()
        cfg = {"agent": {"max_tool_rounds": 1, "max_tool_calls": 5, "max_execution_seconds": 60}}
        result = agent_turn(backend, cfg, [{"role": "user", "content": "hi"}], "system", TOOL_SCHEMAS, confirm_shell=False)
        self.assertIn("stopped", result.lower())


class ConfigValidationTests(unittest.TestCase):
    def test_missing_openai_key_is_rejected(self):
        cfg = {
            "active_backend": "openai",
            "backends": {"openai": {"api_key_env": "OPENAI_API_KEY", "model": "gpt-4.1", "base_url": "https://example.test"}},
            "workspace_root": ".",
            "tools": {"read": True, "write": True, "shell": False},
        }
        with self.assertRaises(ValueError):
            validate_config(cfg)


class WebApiTests(unittest.TestCase):
    def test_health_and_index_endpoints(self):
        from fastapi.testclient import TestClient
        from web.server import app

        client = TestClient(app)
        self.assertIn(client.get("/api/health").status_code, {200, 503})
        self.assertEqual(client.get("/").status_code, 200)

    def test_backend_listing_endpoint(self):
        from fastapi.testclient import TestClient
        from web.server import app

        response = TestClient(app).get("/api/backends")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["backends"])


if __name__ == "__main__":
    unittest.main()
