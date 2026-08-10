"""
Tool implementations the agent can call. Each tool has:
  - a JSON schema (for the LLM to know how to call it)
  - a Python function that actually executes it

The filesystem tools operate inside a configured workspace root and reject
path traversal attempts, absolute paths outside the workspace, and symlink
escapes. Shell execution is bounded and permission-gated.
"""
import os
import subprocess
import sys
from pathlib import Path

TOOL_PERMISSIONS = {
    "read_file": "read",
    "write_file": "write",
    "list_dir": "read",
    "search_code": "read",
    "run_shell": "shell",
    "remember": "write",
    "export_zip": "write",
}

TOOL_SCHEMAS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file at a given path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write (create or overwrite) a file with given content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_dir",
        "description": "List files and folders in a directory.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
        },
    },
    {
        "name": "search_code",
        "description": "Search for a text pattern across files in a directory (like grep -r).",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_shell",
        "description": "Run a shell command and return its output. Use for installs, builds, tests, git, etc.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Save a durable fact, preference, or decision to MM's long-term "
            "memory (memory/notes.md), which is version-controlled and read "
            "at the start of every session. Use this for things worth "
            "recalling later - not for routine chat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": ["Preferences", "Projects", "Decisions & context"],
                    "description": "Which section of memory/notes.md to append under.",
                },
                "note": {"type": "string", "description": "The fact to remember, one line."},
            },
            "required": ["section", "note"],
        },
    },
    {
        "name": "export_zip",
        "description": (
            "Zip a project folder into exports/<name>.zip so the user can "
            "download/share it. Use this once a scaffolded app/web app is "
            "complete, or whenever the user asks to export/package/download "
            "their project."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_dir": {
                    "type": "string",
                    "description": "Path to the project folder to zip, e.g. 'projects/my-app'.",
                },
                "zip_name": {
                    "type": "string",
                    "description": "Output filename without extension, e.g. 'my-app'.",
                },
            },
            "required": ["source_dir", "zip_name"],
        },
    },
]


def resolve_workspace_path(path, workspace_root="."):
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Path must be a non-empty string")
    root = Path(workspace_root).resolve()
    candidate = Path(path)
    if candidate.is_absolute():
        resolved = Path(os.path.realpath(str(candidate)))
    else:
        resolved = Path(os.path.realpath(str(root / candidate)))

    if ".." in Path(path).parts:
        raise ValueError(f"Path traversal is not allowed: {path}")

    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path escapes workspace root: {path}") from exc

    current = root
    for part in resolved.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Symlinks are not allowed: {path}")
    return resolved


def read_file(path, workspace_root=".", max_bytes=20000):
    resolved = resolve_workspace_path(path, workspace_root=workspace_root)
    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        return f"ERROR reading {path}: {exc}"
    if max_bytes and len(content) > max_bytes:
        return content[:max_bytes] + f"\n[truncated: {len(content) - max_bytes} bytes omitted]"
    return content


def write_file(path, content, workspace_root="."):
    resolved = resolve_workspace_path(path, workspace_root=workspace_root)
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as handle:
            handle.write(content)
        return f"Wrote {len(content)} bytes"
    except OSError as exc:
        return f"ERROR writing {path}: {exc}"


def list_dir(path=".", workspace_root="."):
    resolved = resolve_workspace_path(path, workspace_root=workspace_root)
    try:
        return "\n".join(sorted(os.listdir(resolved)))
    except OSError as exc:
        return f"ERROR listing {path}: {exc}"


def search_code(pattern, path=".", workspace_root=".", max_results=50, max_output_chars=10000):
    try:
        root = Path(resolve_workspace_path(path, workspace_root=workspace_root))
    except ValueError as exc:
        return f"ERROR searching: {exc}"

    matches = []
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "venv", ".venv", "node_modules"}]
        for file_name in files:
            file_path = Path(current_root) / file_name
            if file_path.is_symlink():
                continue
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
                    for line_no, line in enumerate(handle, start=1):
                        if pattern in line:
                            matches.append(f"{file_path.relative_to(root)}:{line_no}:{line.rstrip()}")
                            if len(matches) >= max_results:
                                break
            except OSError:
                continue
            if len(matches) >= max_results:
                break
        if len(matches) >= max_results:
            break
    output = "\n".join(matches) if matches else "(no matches)"
    if len(output) > max_output_chars:
        return output[:max_output_chars] + f"\n[truncated: {len(output) - max_output_chars} chars omitted]"
    return output


def run_shell(command, allow_shell=False, timeout_seconds=30, max_output_chars=10000, cwd=None):
    if not allow_shell:
        raise PermissionError("Shell execution is disabled")
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=cwd,
            env={k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "USERPROFILE", "TEMP", "TMPDIR"}},
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Command timed out after {timeout_seconds}s") from exc
    except OSError as exc:
        raise RuntimeError(f"Shell execution failed: {exc}") from exc

    out = (result.stdout or "") + (result.stderr or "")
    if len(out) > max_output_chars:
        out = out[:max_output_chars] + f"\n[truncated: {len(out) - max_output_chars} chars omitted]"
    return out or f"Exit code: {result.returncode}"


def remember(section, note, memory_path=None, workspace_root="."):
    """Append a durable note under the given section heading. Idempotent-ish:
    just appends, doesn't dedupe - the git history is the audit trail."""
    import datetime
    if memory_path is None:
        memory_path = str(resolve_workspace_path("memory/notes.md", workspace_root))
    else:
        memory_path = str(resolve_workspace_path(memory_path, workspace_root))
    Path(memory_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(memory_path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        content = "# MM Memory\n\n## Preferences\n\n## Projects\n\n## Decisions & context\n"

    heading = f"## {section}"
    stamp = datetime.date.today().isoformat()
    entry = f"- ({stamp}) {note}\n"

    if heading in content:
        idx = content.index(heading) + len(heading)
        # insert right after the heading line
        newline_idx = content.index("\n", idx) + 1
        content = content[:newline_idx] + entry + content[newline_idx:]
    else:
        content += f"\n{heading}\n{entry}"

    with open(memory_path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Remembered under '{section}': {note}"


def export_zip(source_dir, zip_name, exports_dir="exports", workspace_root="."):
    """Zip a project folder for the user to download/share."""
    import os as _os
    import zipfile

    source_path = resolve_workspace_path(source_dir, workspace_root)
    exports_path = resolve_workspace_path(exports_dir, workspace_root)
    if not source_path.is_dir():
        return f"ERROR: '{source_dir}' is not a directory."

    if not isinstance(zip_name, str) or not zip_name or Path(zip_name).name != zip_name:
        return "ERROR: zip_name must be a simple filename."
    exports_path.mkdir(parents=True, exist_ok=True)
    zip_path = exports_path / f"{zip_name}.zip"

    skip_dirs = {".git", "__pycache__", "node_modules", "venv", ".venv"}
    file_count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in _os.walk(source_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for f in files:
                full_path = Path(root) / f
                if full_path.is_symlink():
                    continue
                arcname = full_path.relative_to(source_path.parent)
                zf.write(full_path, str(arcname))
                file_count += 1

    return f"Exported {file_count} files to {zip_path}"


TOOL_FUNCTIONS = {
    "read_file": read_file,
    "write_file": write_file,
    "list_dir": list_dir,
    "search_code": search_code,
    "run_shell": run_shell,
    "remember": remember,
    "export_zip": export_zip,
}
