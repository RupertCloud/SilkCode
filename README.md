# Silk Code

**An open, model-agnostic AI coding harness.** Use DeepSeek, Qwen, Kimi, OpenRouter, any
OpenAI-compatible endpoint, or local models (Ollama, vLLM, LM Studio) to understand a
repository, plan changes, write code, run commands and tests, and review diffs — from a
CLI or a local GUI.

> **The coding environment belongs to the developer. The AI model is replaceable.**

The full specification is in [SRS.md](SRS.md). This implementation is V0.1: the Python
agent runtime, the CLI, and the GUI (a local web app served by the Silk Code daemon —
designed to be wrapped in Tauri later, per SRS sections 67-68).

## Install

```bash
pip install -e .          # from this repository
```

Requires Python 3.10+. The only runtime dependency is `httpx`.

## Quick start

```bash
# Cloud model (DeepSeek):
export DEEPSEEK_API_KEY=sk-...
silkcode ~/my-project                      # CLI REPL
silkcode gui ~/my-project                  # GUI at http://127.0.0.1:8377

# Local model (Ollama):
silkcode models pull qwen2.5-coder         # pulls into a running Ollama server
silkcode --model ollama/qwen2.5-coder ~/my-project
```

Then just ask:

```text
silk> Build a small Flask API with tests, and make sure the tests pass.
```

The agent reads and edits files, runs commands and tests, and reports back. File writes
and risky commands ask for your approval first (see *Permissions* below).

## Models: cloud, local, and onboarding your own

Silk Code ships with built-in providers: `deepseek`, `qwen`, `kimi`, `openrouter`,
`ollama`, `vllm`, `lmstudio`. API keys are read from environment variables
(`DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`, `MOONSHOT_API_KEY`, `OPENROUTER_API_KEY`).

```bash
silkcode models                            # list providers, keys, local models
silkcode models pull qwen2.5-coder         # pull a model into Ollama
silkcode models default ollama/qwen2.5-coder

# Onboard any OpenAI-compatible endpoint (private server, enterprise gateway, ...):
silkcode models add myserver \
  --base-url https://ai.company.com/v1 \
  --model my-coder-model \
  --api-key-env MY_API_KEY
silkcode --model myserver
```

Model specs are `provider` (uses its default model) or `provider/model`, e.g.
`deepseek`, `ollama/qwen2.5-coder:32b`, `openrouter/deepseek/deepseek-chat`. Switch
mid-session with `/model <spec>` in the CLI or the model selector in the GUI.
Providers can also be onboarded from the GUI (**+ Add model**).

Configuration lives at `~/.silkcode/config.json` (override the directory with
`$SILKCODE_HOME`).

## CLI

```bash
silkcode [path] [--model M] [--mode ask|edit|agent]   # interactive REPL
silkcode gui [path] [--port N]                        # local GUI
silkcode models [add|pull|default]                    # provider/model management
silkcode sessions                                     # list saved sessions
silkcode resume <id>                                  # continue a session (GUI or CLI)
silkcode test [path] [--command CMD]                  # run the project's tests (auto-detected)
silkcode config                                       # show configuration
```

REPL commands: `/model`, `/models`, `/mode`, `/diff`, `/usage`, `/revert`, `/clear`,
`/sessions`, `/help`, `/exit`.

## GUI

`silkcode gui` starts the Silk Code daemon and opens a browser app with the project
explorer, AI conversation, agent activity timeline, git diff / file viewer, model and
mode selectors, provider onboarding, checkpoint revert, a stop button for running
turns, and live permission prompts. Sessions are saved to the same store as the CLI —
resume any session from the GUI's session picker or with `silkcode resume <id>`
(SRS section 47).

## Permissions and safety

Commands are risk-classified (SRS section 30): read-only commands run automatically;
medium-risk commands (installs, branch switches) need approval unless you are in
`agent` mode; high-risk commands (`rm -rf`, `git push`, destructive checkouts,
`sudo`, ...) always require explicit approval, in every mode.

Modes (SRS section 31): `ask` (approve everything), `edit` (file edits are free,
commands ask), `agent` (autonomous except high-risk).

Before every automated file modification Silk Code snapshots the file; `/revert` (CLI)
or **Revert** (GUI) restores the last turn's changes (SRS section 28).

## Architecture

```
silkcode/
├── providers/       ModelProvider abstraction: OpenAI-compatible + Ollama
├── tools/           read/write/edit, glob/grep, run_command, run_tests, git status/diff/log
├── agent/           the agent loop: streaming, tool dispatch, permissions
├── permissions.py   risk classification + ask/edit/agent modes
├── checkpoints.py   snapshot-before-modify, revert per turn
├── sessions.py      persistence shared by CLI and GUI
├── config.py        provider registry and model resolution
├── cli/             REPL + subcommands
└── gui/             local daemon (HTTP + SSE) + browser app
```

The provider layer is deliberately thin and swappable; the agent runtime is
self-contained and can be replaced or supplemented (e.g. by Deep Agents) behind the
same interfaces, per SRS section 8.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The test suite includes an end-to-end test that drives the real agent and provider
stack against a scripted OpenAI-compatible server — no API keys needed.
