# MM (massive-master) — your private agent

A minimal, fully-yours CLI agent for building full applications and web
applications, coding/debugging help, and general chat/problem-solving.
Swap LLM backends without touching code, define its purpose in one editable
prompt file, and extend it with whatever tools you need.

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
Local models are weaker than frontier cloud models on complex tasks but
keep everything on your machine — good tradeoff to know about.

## Usage

```bash
# interactive
python agent.py

# one-shot: build something
python agent.py "scaffold a full-stack todo web app: FastAPI backend + React frontend"

# one-shot: just talk / think through a problem
python agent.py "should I use Postgres or SQLite for a small internal tool?"
```

MM can read/write files, run shell commands (asks for confirmation first —
you can turn that off in config.yaml), search code, and list directories in
whatever project folder you run it from. For pure discussion/problem-solving
prompts it just responds conversationally, without forcing file/tool use.

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

## Security notes

- `run_shell` asks for confirmation by default — keep this on unless you
  fully trust the prompt/model combo you're running.
- Cloud backends send your code/prompts to that provider's API. If that's
  a concern for company IP, use `ollama_local` or check your company's
  policy on external AI tools first.
- Sessions are saved to `sessions/*.json` in plaintext — don't commit that
  folder to git if it contains sensitive project details.
