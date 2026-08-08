"""
Tool implementations the agent can call. Each tool has:
  - a JSON schema (for the LLM to know how to call it)
  - a Python function that actually executes it

Add new tools by: (1) writing the function, (2) adding it to TOOL_SCHEMAS,
(3) adding it to TOOL_FUNCTIONS.
"""
import os
import subprocess

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


def read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"ERROR reading {path}: {e}"


def write_file(path, content):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"ERROR writing {path}: {e}"


def list_dir(path="."):
    try:
        return "\n".join(sorted(os.listdir(path)))
    except Exception as e:
        return f"ERROR listing {path}: {e}"


def search_code(pattern, path="."):
    try:
        result = subprocess.run(
            ["grep", "-rn", pattern, path],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout or "(no matches)"
    except Exception as e:
        return f"ERROR searching: {e}"


def run_shell(command, confirm=True):
    if confirm:
        answer = input(f"\n[confirm] Run shell command?\n  $ {command}\n  (y/N): ")
        if answer.strip().lower() != "y":
            return "User declined to run this command."
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=120,
        )
        out = result.stdout + result.stderr
        return out[-4000:] if len(out) > 4000 else out  # avoid blowing up context
    except Exception as e:
        return f"ERROR running command: {e}"


def remember(section, note, memory_path="memory/notes.md"):
    """Append a durable note under the given section heading. Idempotent-ish:
    just appends, doesn't dedupe - the git history is the audit trail."""
    import datetime
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


def export_zip(source_dir, zip_name, exports_dir="exports"):
    """Zip a project folder for the user to download/share."""
    import os as _os
    import zipfile

    if not _os.path.isdir(source_dir):
        return f"ERROR: '{source_dir}' is not a directory."

    _os.makedirs(exports_dir, exist_ok=True)
    zip_path = _os.path.join(exports_dir, f"{zip_name}.zip")

    skip_dirs = {".git", "__pycache__", "node_modules", "venv", ".venv"}
    file_count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in _os.walk(source_dir):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for f in files:
                full_path = _os.path.join(root, f)
                arcname = _os.path.relpath(full_path, _os.path.dirname(source_dir))
                zf.write(full_path, arcname)
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
