# Silk Code — Software Requirements Specification

**Document Version:** 0.1
**Product:** Silk Code
**Product Type:** AI Coding Harness / Developer Environment
**Primary Interfaces:** Desktop GUI, CLI
**Target Platforms:** macOS, Windows, Linux
**Initial Runtime:** Python + Deep Agents
**Status:** Draft

---

# 1. Introduction

## 1.1 Purpose

Silk Code is an open, model-agnostic AI coding environment designed to allow developers to use DeepSeek, Qwen, Kimi, GLM, Llama, MiniMax, locally hosted models, and other compatible AI models for software development.

Silk Code shall provide functionality comparable to modern agentic coding tools while avoiding dependence on a single AI provider.

The system shall provide both:

* A full graphical desktop application.
* A command-line interface for terminal-oriented developers.

The graphical interface shall be a first-class product rather than merely a visual wrapper around the CLI.

Silk Code shall allow an AI agent to understand a software repository, plan changes, modify code, execute commands, run tests, inspect errors, manage Git operations, use external tools, and verify its own work.

---

# 2. Product Vision

Silk Code shall become an **open coding operating environment for AI models**.

Instead of a direct relationship such as:

```text
Developer
    ↓
Claude Code
    ↓
Claude
```

Silk Code shall provide:

```text
Developer
       ↓
    Silk Code
       ↓
┌───────────────────────────────┐
│ Agent Runtime                 │
│ Repository Intelligence       │
│ Tool System                   │
│ Context Engine                │
│ Model Router                  │
│ Security / Permissions        │
└──────────────┬────────────────┘
               ↓
┌─────────────────────────────────────────┐
│ DeepSeek | Qwen | Kimi | GLM | Llama   │
│ MiniMax | Ollama | vLLM | Custom APIs  │
└─────────────────────────────────────────┘
```

The core principle is:

> **The coding environment belongs to the developer. The AI model is replaceable.**

---

# 3. Product Goals

Silk Code shall:

1. Provide an easy-to-use AI coding GUI.
2. Provide a powerful CLI for developers.
3. Support multiple AI providers and open-source models.
4. Support locally hosted AI models.
5. Allow developers to switch models without changing development workflows.
6. Automatically understand software repositories.
7. Allow agents to modify and create code.
8. Execute development commands safely.
9. Run tests and inspect failures.
10. Review code changes before they are applied.
11. Support Git workflows.
12. Provide reusable coding skills and instructions.
13. Support MCP and external developer tools.
14. Provide autonomous and supervised coding modes.
15. Allow multiple specialized coding agents.
16. Track model usage, token usage, latency, and cost.
17. Benchmark models against actual software projects.
18. Support enterprise and private development environments.

---

# 4. Non-Goals for Version 1

The first version will not attempt to:

* Replace full IDEs such as VS Code or JetBrains.
* Provide a complete cloud IDE.
* Train foundation AI models.
* Host large GPU clusters directly.
* Automatically deploy production systems without approval.
* Replace GitHub, GitLab, or other source-control providers.

Silk Code shall instead integrate with existing development ecosystems.

---

# 5. Target Users

## 5.1 Individual Developer

A developer working on personal or open-source projects who wants an AI coding assistant without being tied to one provider.

## 5.2 Professional Software Engineer

A developer using Silk Code for production applications, debugging, testing, refactoring, documentation, and feature development.

## 5.3 AI/Open-Model Enthusiast

A user running models locally using:

* Ollama
* vLLM
* LM Studio
* Local OpenAI-compatible servers

## 5.4 Engineering Team

A company requiring:

* Shared configuration
* Approved models
* Centralized permissions
* Usage monitoring
* Private model endpoints
* Audit logs

## 5.5 AI Model Provider

A company or developer wishing to test how well a model performs within an agentic software-development environment.

---

# 6. System Components

Silk Code shall contain the following major subsystems:

```text
Silk Code
│
├── Desktop GUI
│
├── CLI
│
├── Agent Runtime
│
├── Model Router
│
├── Repository Intelligence Engine
│
├── Context Engine
│
├── Tool Runtime
│
├── Git Engine
│
├── Sandbox
│
├── Permission Engine
│
├── Test & Verification Engine
│
├── Skills Engine
│
├── MCP Client
│
├── Session & Memory Engine
│
├── Benchmark Engine
│
└── Telemetry / Usage Engine
```

---

# 7. High-Level Architecture

```text
                    SILK CODE
                       │
           ┌───────────┴───────────┐
           │                       │
           ▼                       ▼
       Desktop GUI                CLI
           │                       │
           └───────────┬───────────┘
                       │
                 Silk Code Core
                       │
        ┌──────────────┼──────────────┐
        │              │              │
        ▼              ▼              ▼
 Agent Runtime     Context Engine   Tool Runtime
        │              │              │
        │              │       ┌──────┴────────┐
        │              │       │ Files         │
        │              │       │ Terminal      │
        │              │       │ Git           │
        │              │       │ Tests         │
        │              │       │ MCP           │
        │              │       │ Browser/API   │
        │              │       └───────────────┘
        │              │
        └──────────────┼─────────────────┐
                       │                 │
                       ▼                 ▼
                  Model Router      Repository Engine
                       │
        ┌──────────────┼───────────────────────┐
        ▼              ▼           ▼           ▼
    DeepSeek          Qwen        Kimi       Local
                                          Ollama/vLLM
```

---

# 8. Technology Architecture

## 8.1 Agent Runtime

The initial implementation should use the **Deep Agents framework** as the underlying agent runtime.

Silk Code shall build its own coding-specific layer on top of the agent runtime.

The Deep Agents dependency shall remain abstracted so that Silk Code can replace or supplement the framework in the future.

Proposed structure:

```text
silkcode/
│
├── core/
│   ├── agents/
│   ├── context/
│   ├── models/
│   ├── router/
│   ├── permissions/
│   ├── repository/
│   └── sessions/
│
├── tools/
│   ├── files/
│   ├── shell/
│   ├── git/
│   ├── testing/
│   ├── search/
│   └── mcp/
│
├── apps/
│   ├── desktop/
│   └── cli/
│
├── providers/
│
├── skills/
│
├── benchmarks/
│
└── sdk/
```

---

# 9. Desktop GUI

The Silk Code GUI shall provide a development environment designed around conversation, agent activity, repository navigation, and change review.

## 9.1 Main Window

The primary workspace shall contain at least six major areas, including the code viewer/editor required by Section 11. Section 66 shows the recommended arrangement.

```text
┌───────────────────────────────────────────────────────────────┐
│ Silk Code     Project ▼        Model: Auto ▼       Settings  │
├──────────────┬─────────────────────────────┬──────────────────┤
│              │                             │                  │
│ PROJECT      │      AI CONVERSATION        │ AGENT ACTIVITY   │
│              │                             │                  │
│ src/         │ > Fix authentication bug    │ ✓ Search repo    │
│ tests/       │                             │ ✓ Read files     │
│ package.json │ Silk Code:                  │ ● Editing        │
│              │ I found the issue...        │ ○ Run tests      │
│              │                             │                  │
├──────────────┴─────────────────────────────┴──────────────────┤
│ CODE / DIFF VIEWER                                            │
├───────────────────────────────────────────────────────────────┤
│ TERMINAL / PROBLEMS / TESTS / GIT / OUTPUT                   │
└───────────────────────────────────────────────────────────────┘
```

---

# 10. GUI Functional Requirements

## FR-GUI-001 — Project Selection

The user shall be able to:

* Open an existing folder.
* Open a Git repository.
* Clone a repository.
* Open recently used projects.
* Create a new project.

---

## FR-GUI-002 — Repository Explorer

The application shall display:

* Folder hierarchy
* Files
* Git status
* Modified files
* Added files
* Deleted files
* Ignored files

The explorer shall support:

* Create file
* Rename
* Delete
* Move
* Copy path
* Search
* Open file

---

# 11. Code Viewer and Editor

The GUI shall contain a source-code viewer/editor.

The editor should support:

* Syntax highlighting
* Line numbers
* Tabs
* Multiple open files
* Search
* Replace
* Go to line
* Code selection
* Copy
* Editing
* Diagnostics
* Diff visualization

Silk Code does not initially need to reproduce every IDE feature.

The editor shall primarily allow the developer to inspect and adjust AI-generated code.

---

# 12. AI Conversation Interface

The main interaction area shall provide a ChatGPT/Claude-Code-style conversation.

Example:

```text
You

Build password reset functionality.

Silk Code

I'll inspect the authentication implementation first.

● Searching repository
● Reading 6 files
● Reviewing database schema

I found an existing token service that we can extend.

Proposed changes:

1. Add password reset tokens.
2. Add reset request endpoint.
3. Add password update endpoint.
4. Add tests.

[Proceed]
```

The user shall be able to:

* Enter natural-language requests.
* Attach files.
* Reference files using `@filename`.
* Reference folders.
* Reference Git changes.
* Reference terminal output.
* Reference errors.
* Reference screenshots where supported.
* Stop an agent.
* Continue an interrupted task.

---

# 13. Prompt Commands

Silk Code shall support commands such as:

```text
/model
/models
/plan
/review
/test
/fix
/explain
/commit (V0.2 — see Section 79)
/diff
/context
/agents
/skills
/benchmark
/settings
```

Example:

```text
/model deepseek
```

or:

```text
/model auto
```

---

# 14. Agent Activity Panel

The GUI shall visually display what the agent is doing.

Example:

```text
AGENT ACTIVITY

✓ Repository scanned

✓ Read
  src/auth/login.ts

✓ Read
  src/auth/token.ts

✓ Search
  refreshToken

● Editing
  src/auth/token.ts

○ Run tests

○ Review changes
```

The user shall be able to expand each activity and inspect its details.

---

# 15. Model Selector

The GUI shall contain a model selector.

Example:

```text
Model

● Auto

Cloud
  DeepSeek V4
  Qwen
  Kimi
  GLM
  MiniMax

Local
  Qwen Coder 32B
  DeepSeek Coder
  Llama
```

Each model may display:

* Provider
* Context window
* Cost
* Speed
* Local/cloud status
* Tool capability
* Reasoning capability

---

# 16. Auto Model Selection

Silk Code shall support:

```text
Model: AUTO
```

In Auto mode, Silk Code may select different models for different operations.

Example:

```text
Task: Build Stripe integration

Planner
DeepSeek

Repository Search
Qwen Local

Implementation
DeepSeek

Tests
Qwen Local

Code Review
Kimi
```

---

# 17. Model Router

The Model Router shall select models based on:

* Task type
* Complexity
* Context size
* Tool support
* User preference
* Latency
* Cost
* Privacy requirements
* Benchmark scores
* Model availability

Example internal request:

```json
{
  "task": "code_review",
  "complexity": "medium",
  "privacy": "local_preferred",
  "context_tokens": 34000
}
```

---

# 18. Model Provider System

Silk Code shall expose a provider abstraction.

Conceptual interface:

```python
class ModelProvider:
    def chat(self, messages, tools):
        ...

    def stream(self, messages, tools):
        ...

    def capabilities(self):
        ...
```

Supported providers shall eventually include:

* DeepSeek
* Qwen
* Kimi
* GLM
* MiniMax
* OpenAI-compatible endpoints
* Ollama
* vLLM
* LM Studio
* OpenRouter-compatible gateways
* Custom enterprise endpoints

---

# 19. Provider Configuration

Users shall be able to configure providers through the GUI.

Example:

```text
Providers

DeepSeek
API Key: ************
Status: Connected

Ollama
URL: http://localhost:11434
Status: Connected

Private AI Server
URL: https://ai.company.com/v1
API Key: ************
Status: Connected
```

Credentials shall be stored securely using operating-system credential storage where available.

---

# 20. Local AI Models

Silk Code shall treat local models as first-class models.

Users shall be able to connect to:

```text
Ollama
vLLM
LM Studio
OpenAI-compatible localhost endpoints
```

The GUI shall indicate:

```text
LOCAL

Qwen Coder 32B
Privacy: Device only
Cost: Free
Status: Running
```

---

# 21. Repository Intelligence Engine

Silk Code shall create an internal representation of a codebase.

Repository analysis shall include:

* Directory structure
* Languages
* Frameworks
* Dependency files
* Symbols
* Classes
* Functions
* Imports
* References
* Git history
* Test locations
* Configuration files

---

# 22. Repository Map

Silk Code shall generate a compact repository map.

Example:

```text
Application
│
├── src/auth
│   ├── AuthController
│   ├── LoginService
│   └── TokenService
│
├── src/payments
│   ├── PaymentService
│   └── StripeProvider
│
└── tests
```

The repository map shall help agents identify relevant files without loading the entire project.

---

# 23. Repository Search

Agents shall be able to perform:

* Filename search
* Text search
* Regex search
* Symbol search
* Definition search
* Reference search

Initial search may use tools such as:

```text
ripgrep
tree-sitter
```

Semantic indexing may be introduced later.

---

# 24. Context Engine

The Context Engine shall determine what information is sent to an AI model.

Potential context includes:

* User conversation
* Relevant files
* Repository map
* Current Git diff
* Terminal errors
* Test failures
* Project instructions
* Agent memory
* Symbols
* Dependencies

Silk Code shall avoid sending entire repositories where unnecessary.

---

# 25. Context Inspector

The GUI shall allow developers to inspect the AI context.

Example:

```text
CONTEXT

18,241 / 128,000 tokens

Included:

✓ src/auth/login.ts
✓ src/auth/token.ts
✓ tests/auth.test.ts
✓ package.json
✓ Git diff
✓ project instructions
```

Users shall be able to:

* Add files
* Remove files
* Pin files
* Clear context

---

# 26. File Operations

The agent shall support:

* Read
* Create
* Edit
* Patch
* Rename
* Move
* Delete
* Search
* Glob

All modifications shall be tracked.

---

# 27. AI Change Review

Before or after changes, Silk Code shall display a visual diff.

Example:

```diff
- const token = jwt.sign(user, secret)
+ const token = jwt.sign(user, secret, {
+   expiresIn: "15m"
+ })
```

The user shall be able to:

* Accept file
* Reject file
* Accept individual changes
* Revert changes
* Ask AI to modify change
* Explain change

---

# 28. Checkpoints

Silk Code shall create checkpoints before significant automated modifications.

Example:

```text
Checkpoint

Before: Authentication Fix
12 Aug 2026 — 11:31

[Restore]
```

Users shall be able to restore previous workspace states.

---

# 29. Terminal

The GUI shall provide an integrated terminal.

The agent shall be allowed to execute commands based on permissions.

Examples:

```bash
npm install
npm test
pytest
flutter test
cargo test
git status
```

---

# 30. Shell Safety

Commands shall be classified by risk.

### Low Risk

```text
ls
pwd
git status
npm test
pytest
```

May execute automatically.

### Medium Risk

```text
npm install
git checkout (when no uncommitted changes would be affected)
database migrations
```

May require approval depending on settings.

### High Risk

```text
rm -rf
git checkout / git restore discarding uncommitted changes
git push --force
production deployment
credential changes
```

Shall require explicit permission unless enterprise policy explicitly permits otherwise.

---

# 31. Permission Modes

Silk Code shall provide at least four permission modes.

## Ask

Ask before any modification or command.

## Edit

Allow file editing but request approval for terminal commands.

## Agent

Allow normal development operations autonomously while requesting approval for dangerous operations.

## Custom

Allow the user or organization to define detailed policies.

---

# 32. Git Integration

Silk Code shall provide native Git functionality.

The GUI shall display:

* Current branch
* Changed files
* Staged files
* Commits
* Diff
* Repository status

The AI shall be able to:

* Inspect history
* Create branches
* Stage changes
* Generate commit messages
* Commit changes

Inspection and diff capabilities are required for V0.1; branch, stage, and commit operations are scheduled for V0.2 (Section 79).

Remote operations such as push should follow configured permission policies.

---

# 33. Git Panel

Example:

```text
SOURCE CONTROL

Branch
feature/auth-reset

Changes 3

M src/auth/login.ts
M src/auth/token.ts
A tests/password-reset.test.ts

[Review Changes]

Commit Message

Add password reset workflow

[Commit]
```

---

# 34. Testing Engine

Silk Code shall detect common testing frameworks.

Examples:

* Jest
* Vitest
* PyTest
* Go test
* Cargo
* Flutter Test
* PHPUnit
* Maven
* Gradle

Agents shall be able to:

1. Make changes.
2. Run tests.
3. Read failures.
4. Fix failures.
5. Re-run tests.
6. Report final results.

---

# 35. Problems Panel

The GUI shall provide:

```text
PROBLEMS

3 errors
2 warnings

auth.ts:54
Property 'token' does not exist.

payments.ts:81
Possible undefined value.
```

Users may select:

```text
Fix with Silk Code
```

---

# 36. Planning Mode

Users shall be able to request planning without modification.

```text
/plan Implement team-based permissions
```

Silk Code shall analyze the repository and return:

```text
Implementation Plan

1. Extend user schema.
2. Add teams table.
3. Add role middleware.
4. Update authorization.
5. Add API endpoints.
6. Add tests.

Estimated files affected: 11

[Start Implementation]
```

---

# 37. Code Review Mode

Silk Code shall support:

```text
/review
```

The agent shall inspect:

* Current diff
* Potential bugs
* Security problems
* Performance issues
* Missing tests
* Style problems
* Breaking changes

---

# 38. Debugging Mode

Users shall be able to submit errors.

Example:

```text
> Fix this error

TypeError: Cannot read properties of undefined
```

Silk Code shall:

1. Search the repository.
2. Locate relevant code.
3. Identify likely cause.
4. Propose or apply a fix.
5. Run relevant tests.
6. Report outcome.

---

# 39. Multiple Agents

Silk Code shall support subagents.

Example:

```text
Lead Agent
│
├── Backend Agent
├── Frontend Agent
├── Testing Agent
├── Security Agent
└── Reviewer Agent
```

Each agent may use a different model.

---

# 40. Agent Dashboard

The GUI may display:

```text
AGENTS

Lead
DeepSeek
Planning architecture
● Running

Frontend
Qwen
Updating UI
● Running

Tests
Local Qwen
Waiting for frontend
○ Waiting

Reviewer
Kimi
○ Waiting
```

---

# 41. Coding Skills

Silk Code shall support reusable skills.

Examples:

```text
React Expert
Flutter Expert
Django Expert
Laravel Expert
Security Reviewer
API Designer
Database Migration
Documentation Writer
Test Engineer
```

Skills shall include:

* Instructions
* Relevant tools
* Preferred models
* Coding standards
* Verification requirements

---

# 42. Project Instructions

Projects may contain:

```text
SILKCODE.md
```

Example:

```markdown
# Project Instructions

Use TypeScript.

Never use `any`.

Run tests after modifying authentication.

Use PostgreSQL.

Do not modify production configuration.
```

Silk Code shall automatically load these instructions.

---

# 43. Memory

Silk Code shall provide project memory.

Memory may include:

* Architecture decisions
* Coding conventions
* Frequently modified areas
* User preferences
* Important commands
* Known limitations

Memory shall be inspectable and editable.

---

# 44. MCP Integration

Silk Code shall support Model Context Protocol integrations.

Potential tools include:

* GitHub
* GitLab
* Jira
* Linear
* Databases
* Documentation
* Cloud environments
* Monitoring systems

Users shall be able to enable or disable MCP servers.

---

# 45. CLI

The CLI shall provide the same fundamental agent engine as the GUI.

Launch:

```bash
silkcode
```

Example:

```text
Silk Code

Model: Auto
Project: ~/projects/ridelink

> Fix the failing authentication tests.

● Reading repository
● Running tests
● 3 failures detected
● Inspecting authentication
● Editing 2 files
● Running tests

✓ 124 tests passed

Changed:
M src/auth/token.ts
M tests/auth.test.ts
```

---

# 46. CLI Commands

Examples:

```bash
silkcode
silkcode .
silkcode --model deepseek
silkcode --model ollama/qwen
silkcode review
silkcode test
silkcode benchmark
silkcode models
silkcode config
silkcode sessions
silkcode resume <session-id>
```

---

# 47. GUI/CLI Session Sharing

A user shall be able to begin work in the GUI and continue in the CLI.

Example:

```bash
silkcode sessions
```

```text
#128 Auth Fix
#127 Payment API
#126 Dashboard redesign
```

Then:

```bash
silkcode resume 128
```

---

# 48. Usage Dashboard

Silk Code shall track:

* Tokens
* Requests
* Cost
* Model
* Provider
* Response time
* Tasks completed

GUI example:

```text
TODAY

Requests     46
Tokens       781K
Cost         $1.84

Models

DeepSeek     $1.21
Kimi         $0.43
Qwen Local   $0.00
Other        $0.20
```

---

# 49. Cost Limits

Users shall be able to define limits.

Examples:

```text
Maximum task cost: $1
Daily limit: $10
Monthly limit: $100
```

When limits are reached, Silk Code may:

* Ask the developer
* Switch to local models
* Switch to cheaper models
* Stop the task

---

# 50. Model Benchmarking

Silk Code shall include a benchmark system.

Command:

```bash
silkcode benchmark
```

Users may evaluate configured models on:

* Bug fixing
* Repository understanding
* Code generation
* Test generation
* Tool use
* Refactoring
* Code review

Example:

```text
MODEL BENCHMARK

                    Success   Time   Cost

DeepSeek              91%     28s    $0.03

Kimi                  88%     34s    $0.04

Qwen Local            81%     61s    $0.00
```

---

# 51. Project-Specific Model Ranking

Silk Code should eventually learn which models perform best for each project.

Example:

```text
Recommended for this repository

1. DeepSeek — 94%
2. Qwen Coder — 91%
3. Kimi — 87%
```

These scores should be based on actual successful tasks and evaluations rather than marketing claims.

---

# 52. Model Profiles

Silk Code shall support model-specific optimization.

Example:

```text
profiles/

deepseek.yaml
qwen.yaml
kimi.yaml
glm.yaml
llama.yaml
```

Profiles may configure:

* System prompts
* Tool descriptions
* Context strategy
* Patch strategy
* Retry behavior
* Reasoning settings
* Parallel tool use
* Verification strategy

---

# 53. Task History

Every agent request shall create a task.

Example:

```text
TASK HISTORY

Today

11:31
Fix authentication refresh
✓ Completed

10:54
Review payment API
✓ Completed

Yesterday

18:30
Implement dashboard charts
✕ Cancelled
```

---

# 54. Task Details

A task shall store:

* User request
* Model(s)
* Tools used
* Files read
* Files changed
* Commands executed
* Test results
* Cost
* Token usage
* Duration
* Final response

---

# 55. Session Persistence

Initial local persistence may use SQLite.

Potential tables:

```text
projects
sessions
messages
tasks
tool_calls
models
providers
usage
permissions
memories
checkpoints
benchmarks
skills
```

---

# 56. Notifications

The GUI shall notify users of important events.

Examples:

```text
✓ Tests completed successfully.

⚠ Silk Code requires permission to install 4 packages.

⚠ Agent wants to modify database schema.

✕ Build failed.
```

---

# 57. Settings

The GUI shall include settings for:

## General

* Theme
* Language
* Startup behavior

## Models

* Providers
* API keys
* Model preferences

## Agent

* Autonomy
* Planning
* Verification
* Maximum iterations

## Permissions

* Shell
* Files
* Git
* Network
* MCP

## Context

* Context size
* Indexing
* Memory

## Privacy

* Telemetry
* Cloud requests
* Local-only mode

---

# 58. Privacy Mode

Silk Code shall offer:

```text
Local Only Mode
```

When enabled:

* No source code shall be sent to external model providers.
* Only local AI endpoints may be used.
* Cloud telemetry shall be disabled unless explicitly permitted.
* External network tools shall require explicit permission.

---

# 59. Enterprise Policy

Organizations should eventually be able to define policies such as:

```text
Allowed models:
✓ Company Qwen
✓ DeepSeek Enterprise

Blocked:
✕ Public models

Shell:
Ask for destructive operations

Source code:
Never leave company infrastructure
```

---

# 60. Security Requirements

Silk Code shall:

* Encrypt secrets where applicable.
* Avoid exposing API keys in prompts.
* Prevent automatic access to sensitive files.
* Allow `.env` exclusion.
* Support path permission rules.
* Log important agent actions.
* Require approval for dangerous operations, unless enterprise policy explicitly permits otherwise (see Section 30).
* Provide command allow/deny policies.
* Provide network access controls.

---

# 61. Sensitive File Protection

Default protected patterns should include:

```text
.env
.env.*
*.pem
*.key
credentials.*
~/.ssh/**
```

Reading protected files should require explicit approval or policy permission.

---

# 62. Sandbox

Silk Code should support multiple execution environments.

```text
Local Workspace
Local Sandbox
Docker
Remote Sandbox
Enterprise Sandbox
```

The sandbox interface shall remain provider-independent.

---

# 63. Remote Development

Later versions may support remote coding environments.

Example:

```text
Silk Code Desktop
       ↓
Silk Sandbox Cloud
       ↓
Repository
       ↓
Agent
```

This shall be optional.

---

# 64. Status Bar

The GUI status bar should display information such as:

```text
main | DeepSeek | Agent Mode | Context 18% | $0.18
```

---

# 65. Welcome Screen

Example:

```text
Silk Code

Code with any AI.

[Open Project]

[Clone Repository]

[New Project]


Recent Projects

Ridelink Platform
Silk AI
FMG Portal
```

---

# 66. Recommended GUI Layout

The recommended initial GUI is:

```text
┌──────────────────────────────────────────────────────────────┐
│ Silk Code | Project | Model | Agent Mode | Cost | Settings  │
├─────────────┬────────────────────────────┬───────────────────┤
│             │                            │                   │
│ Repository  │         AI Chat            │ Agent Timeline    │
│             │                            │                   │
│             │                            │                   │
│             │                            │                   │
├─────────────┼────────────────────────────┴───────────────────┤
│ Open Files  │ Code / Diff Viewer                            │
│             │                                               │
├─────────────┴────────────────────────────────────────────────┤
│ Terminal | Tests | Problems | Git | Output                  │
└──────────────────────────────────────────────────────────────┘
```

Panels should be resizable and hideable.

---

# 67. Desktop Framework

Recommended initial desktop stack:

```text
Frontend
React
TypeScript

Desktop Shell
Tauri

Editor
Monaco Editor

Styling
Tailwind CSS or equivalent

Agent Backend
Python

IPC
Local HTTP / WebSocket / Tauri commands

Database
SQLite
```

Tauri is preferred over building the entire desktop application directly in Python because it provides a modern desktop experience while allowing the agent runtime to remain Python-based.

---

# 68. Proposed Internal Architecture

```text
              Tauri Desktop
                   │
             React GUI
                   │
                IPC/API
                   │
          Silk Code Daemon
               Python
                   │
        ┌──────────┼──────────┐
        │          │          │
   Deep Agents   Tools     Repository
        │          │          │
        └──────────┼──────────┘
                   │
              Model Router
                   │
      ┌────────────┼────────────┐
      ▼            ▼            ▼
  DeepSeek       Ollama       Custom
```

The same backend daemon can power both GUI and CLI.

---

# 69. Silk Code Daemon

A local service should expose the core engine.

Potential interfaces:

```text
POST /sessions

POST /sessions/{id}/messages

GET /sessions/{id}/events

POST /tasks/{id}/cancel

GET /projects/{id}/files

GET /projects/{id}/git

GET /models

POST /models/test
```

WebSockets or server-sent events should be used for streaming agent activity.

---

# 70. Event System

The runtime shall publish events.

Example:

```json
{
  "type": "tool.started",
  "tool": "read_file",
  "file": "src/auth/login.ts"
}
```

Other events:

```text
agent.started
agent.message
tool.started
tool.completed
tool.failed
file.changed
command.started
command.output
command.completed
test.completed
permission.requested
checkpoint.created
task.completed
```

The GUI shall subscribe to these events.

---

# 71. Performance Requirements

Silk Code should:

* Launch the GUI within a few seconds on supported modern hardware.
* Stream model output as it arrives.
* Display tool activity without waiting for task completion.
* Avoid freezing the GUI during agent operations.
* Handle repositories containing tens of thousands of files.
* Index repositories incrementally.
* Cache repository metadata.

---

# 72. Reliability Requirements

The system shall:

* Recover sessions after application restart.
* Preserve code if the AI process crashes.
* Keep checkpoints before major modifications.
* Allow task cancellation.
* Handle provider outages.
* Retry transient provider errors.
* Support provider fallback.

---

# 73. Provider Failure Handling

Example:

```text
DeepSeek unavailable.

Silk Code can continue with:

Qwen Local
Kimi
GLM

[Use Qwen Local]
```

Auto mode may perform fallback automatically when permitted.

---

# 74. Offline Capability

Silk Code shall support fully offline operation when:

* A local model is available.
* Required dependencies have already been installed.
* Cloud integrations are disabled.

Core repository operations, Git, terminal, and agent functionality shall remain available.

---

# 75. Accessibility

The GUI should support:

* Keyboard navigation
* Adjustable font sizes
* High contrast compatibility
* Screen-reader labels
* Configurable shortcuts

---

# 76. Keyboard Shortcuts

Suggested shortcuts:

```text
Ctrl/Cmd + K       Ask Silk Code
Ctrl/Cmd + P       File search
Ctrl/Cmd + Shift+P Command palette
Ctrl/Cmd + `       Terminal
Ctrl/Cmd + Enter   Send
Esc                Stop agent
```

---

# 77. Command Palette

The GUI shall provide:

```text
Silk Code: Change Model
Silk Code: Start Plan
Silk Code: Review Changes
Silk Code: Run Tests
Silk Code: Open Agent Timeline
Silk Code: Create Checkpoint
Silk Code: Restore Checkpoint
```

---

# 78. MVP Requirements

Silk Code V0.1 shall include:

* Desktop GUI.
* CLI.
* Open local folder.
* Repository explorer.
* AI conversation.
* DeepSeek integration.
* OpenAI-compatible provider integration.
* Ollama integration.
* Model selector.
* Read files.
* Write files.
* Edit files.
* Repository search.
* Terminal execution.
* Git diff.
* Test execution.
* Permission prompts.
* Code diff review.
* Basic checkpoints (create before automated modifications; restore/revert).
* Session persistence.
* GUI/CLI session sharing (`silkcode sessions`, `silkcode resume`).
* Agent activity timeline.
* Basic usage statistics.

---

# 79. V0.2 Requirements

The next version should add:

* Qwen optimization.
* Kimi.
* GLM.
* MiniMax.
* Model Auto Router.
* Repository maps.
* Tree-sitter symbol indexing.
* Skills.
* Project memory.
* MCP.
* Git commits.
* Advanced checkpoints.
* Model benchmarking.

---

# 80. V0.3 Requirements

Potential V0.3 functionality:

* Multi-agent development.
* Different models per agent.
* Remote sandboxes.
* Enterprise policies.
* Team settings.
* Shared skills.
* Organization model gateway.
* Audit logs.
* GitHub/GitLab integration.
* Pull-request review.

---

# 81. Future Vision

Long-term Silk Code may evolve into:

```text
                    SILK CODE
                        │
       ┌────────────────┼────────────────┐
       ▼                ▼                ▼
    Desktop            CLI             IDE
       │                │                │
       └────────────────┼────────────────┘
                        │
                   Agent OS
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
      Local           Cloud         Enterprise
      Models          Models          Models
```

Silk Code may eventually allow organizations to operate a complete internal AI software-engineering workforce using privately hosted models.

---

# 82. Competitive Differentiation

Silk Code should not position itself simply as:

> Claude Code with DeepSeek.

The intended positioning is:

> **Silk Code is the open coding environment for any AI model.**

Key differentiation shall include:

* Open-model-first design.
* GUI + CLI.
* Local-model support.
* Provider independence.
* Automatic model routing.
* Multi-model workflows.
* Model benchmarking.
* Privacy-first operation.
* Model-specific optimization profiles.
* Multi-agent development.
* Transparent cost reporting.

---

# 83. Core Product Principle

The central architecture should ensure:

```text
Models are replaceable.

Tools are reusable.

Context is controlled by Silk Code.

Permissions are controlled by the developer.

The repository remains the source of truth.
```

This separation is critical to the long-term architecture.

---

# 84. Success Criteria for V0.1

Silk Code V0.1 shall be considered successful when a developer can:

1. Install Silk Code.

2. Open a repository through the GUI.

3. Configure DeepSeek or Ollama.

4. Select a model.

5. Ask:

   `Fix the login bug.`

6. See Silk Code inspect the repository.

7. Watch file and tool activity in real time.

8. Review proposed code changes.

9. Allow the AI to run tests.

10. See whether the tests passed.

11. Review the resulting Git diff.

12. Accept or revert the changes.

13. Continue the same session from the CLI.

14. Switch to another AI model without changing the project or workflow.

That represents the minimum complete Silk Code experience.

---

# 85. Product Definition

**Silk Code** is a model-independent AI software-development environment combining a visual desktop coding workspace, developer CLI, autonomous agent runtime, repository intelligence, tool execution, Git integration, testing, model routing, local AI support, and multi-model workflows.

Its core promise is:

> **Use the best AI model for every coding task — cloud or local — from one coding environment.**
