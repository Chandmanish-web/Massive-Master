# MM (massive-master) — your private agent

A provider-independent local coding agent for building apps, debugging code,
and working through problems safely. The agent now supports Anthropic,
OpenAI, and Ollama through a shared internal protocol, while filesystem and
shell tools are constrained by a workspace sandbox and explicit permissions.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate      # on Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Pick a backend in `config.yaml` (`active_backend: anthropic|openai|ollama_local`).

### Cloud backend (Anthropic or OpenAI)
```bash
export ANTHROPIC_API_KEY=sk-ant-...
# or
export OPENAI_API_KEY=sk-...
```

### Fully local/offline backend (nothing leaves your laptop)
```bash
# 1. install Ollama: https://ollama.com
# 2. pull a coding model
ollama pull qwen2.5-coder:14b
# 3. set active_backend: ollama_local in config.yaml
```
Ollama tool calling is supported when the HTTP API is available. CLI fallback
is disabled by default and only used when explicitly configured.

## Usage

### Browser chat UI (like Claude / ChatGPT)

```bash
python -m web.server
```
Open **http://localhost:8000** — a chat interface, running entirely on your
laptop, talking to whichever backend you configured. Sessions persist locally in the configured sessions directory. The web UI does **not** expose `run_shell` by
default (no safe way to prompt for confirmation mid-request) — enable it
explicitly via `web_enable_shell: true` in `config.yaml` if you want it,
but understand that means MM can execute shell commands with no
confirmation step when reached through the browser.

### CLI

```bash
# interactive
python agent.py

# one-shot: build something
python agent.py "scaffold a full-stack todo web app: FastAPI backend + React frontend"

# one-shot: just talk / think through a problem
python agent.py "should I use Postgres or SQLite for a small internal tool?"
```

MM can read/write files, search code, and list directories inside the
configured workspace root. Shell execution is disabled by default and only
runs when explicitly allowed in config and approved by the user. For pure
discussion/problem-solving prompts it responds conversationally without
forcing file/tool use.

## Customizing "what it's for"

Edit `prompts/system_prompt.md`. This single file controls the agent's
purpose, tone, and constraints — e.g. narrow it to "only work inside
/company-projects and always write tests" or broaden it to anything.

## Adding new tools

In `tools.py`:
1. Write the function.
2. Add its JSON schema to `TOOL_SCHEMAS`.
3. Add it to `TOOL_FUNCTIONS`.

Examples of tools worth adding for company work: git commit/push, Docker
build/run, hitting an internal API, deploying to a staging server, reading
from a company wiki or ticket system.

## Building & exporting apps

Ask MM to build something and it will scaffold a real project (backend,
frontend, config, README) under a folder of your choosing — e.g.:

```bash
python agent.py "build a task tracker: FastAPI backend, React frontend, SQLite db. Put it in projects/task-tracker"
```

MM applies real UI/UX judgment by default (see `prompts/system_prompt.md`
for the exact standards it follows) — deliberate color/type choices,
responsive layout, accessible markup — rather than shipping unstyled
framework defaults.

Once a build is complete, MM will zip it via the `export_zip` tool into
`exports/<name>.zip`, ready to download or hand off. You can also ask for
this explicitly any time: *"export projects/task-tracker as a zip."*

`projects/` and `exports/*.zip` are gitignored — they're build outputs,
not part of MM's own tracked codebase/memory.

## Memory & git history

MM is version-controlled with git from the start, and that history *is*
its long-term memory:

- Every session's full conversation is saved locally to `sessions/session_<date>.json` (ignored by git by default).
- MM can call the `remember` tool to save durable facts (your preferences,
  project details, decisions) into `memory/notes.md` — a plain, human-readable
  file organized by section.
- Automatic git commits are disabled by default. If enabled, only explicitly
  allow-listed source changes are committed; sessions, memory, secrets, and
  generated artifacts are never added automatically. Commit failures are
  reported instead of being silently ignored.
- Memory is local runtime data and is read fresh into the system prompt at the
  start of every session.

Useful commands:
```bash
git log --oneline                    # full timeline of everything MM did
git log -p memory/notes.md           # see how MM's memory of you evolved
git diff HEAD~5 -- memory/notes.md   # what got remembered in the last 5 commits
```

If you ever want to fork this into a *shared* team knowledge base rather
than a personal one, push this repo to a private GitHub/GitLab remote —
the structure already supports it.

## Security notes

- Filesystem tools are sandboxed to the configured workspace root and reject
  traversal, absolute paths outside the workspace, and symlink escapes.
- Shell execution requires explicit approval and is disabled by default.
- Cloud backends send your code/prompts to that provider's API. If that's
  a concern for company IP, use `ollama_local` or check your company's
  policy on external AI tools first.
- Session data is no longer tracked by git by default; runtime data is stored
  under the configured session directory and should be treated as sensitive.
