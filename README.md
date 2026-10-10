<div align="center">

<img src="assets/mindsync-logo.png" alt="MindSync AI Logo" width="420" />

# MindSync AI

**Local-first MCP orchestration, persistent shared memory, and automatic task routing for coding agents.**

[![CI](https://github.com/adityarya24/mindsync-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/adityarya24/mindsync-ai/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/mindsync-ai.svg)](https://pypi.org/project/mindsync-ai/)
[![PyPI downloads](https://static.pepy.tech/badge/mindsync-ai)](https://pepy.tech/project/mindsync-ai)
[![Python versions](https://img.shields.io/pypi/pyversions/mindsync-ai.svg)](https://pypi.org/project/mindsync-ai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![MCP Compatible](https://img.shields.io/badge/MCP-Protocol%201.27+-purple.svg)](https://modelcontextprotocol.io/)

### 🌐 [adityarya24.github.io/mindsync-ai](https://adityarya24.github.io/mindsync-ai/)

</div>

---

> [!NOTE]
> **This is the open core of MindSync.** It stays MIT-licensed and keeps getting bug fixes, security fixes and small improvements. MindSync Platform, a commercial product for coordinating many agents, is built on it and is in private development. See [COMMUNITY_STATUS.md](COMMUNITY_STATUS.md) for what lands where.

---

## 💡 What is MindSync?

MindSync connects disparate AI coding agents (**Codex, Claude Code, Gemini CLI, Antigravity, Grok, Cursor, OpenCode, Aider**) into a single, coordinated local ecosystem without requiring a cloud SaaS account or third-party servers.

The human-facing CLI session you are talking to **remains in charge as the orchestrator**. MindSync routes subtasks by domain capability, enforces file locks to prevent multi-agent collisions, skips providers that are temporarily cooling down, and preserves long-term factual memory across sessions.

```
                    ┌────────────────────────┐
                    │   You (Human Prompt)   │
                    └───────────┬────────────┘
                                │
                                ▼
         ┌────────────────────────────────────────────────┐
         │     Human-Facing CLI Session (Orchestrator)    │
         │  (e.g., Codex / Claude / Gemini / Grok / ...)   │
         └──────────────────────┬─────────────────────────┘
                                │ (MCP Protocol)
                                ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                        MINDSYNC CORE                        │
  │  ┌───────────────────────┬───────────────────────────────┐  │
  │  │  Capability Router    │  Conflict Prevention Shield   │  │
  │  ├───────────────────────┼───────────────────────────────┤  │
  │  │  Reactive Cooldowns   │  Vector Memory (`sqlite-vec`) │  │
  │  └───────────────────────┴───────────────────────────────┘  │
  └─────────────────────────────┬───────────────────────────────┘
                                │ (Isolated Worktrees)
         ┌──────────────────────┼──────────────────────┐
         ▼                      ▼                      ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│   Codex Worker   │  │  Claude Worker   │  │  Gemini / Grok   │
│  (Implementation)│  │ (Deep Reasoning) │  │ (Audit & Search) │
└──────────────────┘  └──────────────────┘  └──────────────────┘
```

> **Key Guarantee**: Dispatched workers receive bounded tasks inside isolated Git worktrees and cannot recursively re-delegate through MindSync.

---

## ⚡ Quick Start

### 1. Installation
```bash
pip install mindsync-ai
```
*(Requires Python 3.10+)*

### 2. Automated Auto-Discovery & Setup
```bash
# Auto-detects installed MCP hosts and PATH coding CLIs
mindsync setup --mode auto

# Verify system health, lock engines, and adapter status
mindsync doctor

# Inspect available agent roster and capabilities
mindsync agents
```
*Restart your CLI sessions after setup to load the registered MCP servers.*

```bash
# Additional setup options
mindsync setup --dry-run          # Preview changes without modifying host configs
mindsync setup --cli grok         # Target a specific host only
mindsync setup --no-discover      # Register hosts without PATH CLI scanning
mindsync setup --no-hooks         # Skip Codex standalone hooks
```

---

## 🤖 Supported Clients & Roster

A CLI may act as an **MCP Host** (orchestrator), a **Worker** (dispatched execution), or both:

| CLI / Engine | MCP Host | Dispatched Worker | Domain Strength / Role |
| :--- | :---: | :---: | :--- |
| **OpenAI Codex** | Native | Yes | Fast implementation, refactoring, standalone memory hooks |
| **Anthropic Claude Code** | Native | Yes | Architecture, comprehensive reviews, massive context |
| **Google Gemini CLI** | Native | Yes | Research, multimodal analysis, tool integrations |
| **Antigravity (`agy`)** | Via Gemini | Yes | Preferred execution worker in Gemini family |
| **Grok CLI (xAI)** | Native | Yes | Codebase exploration, security audit, rapid synthesis |
| **Cursor Agent** | JSON (`mcp.json`) | Yes | In-IDE pair programming, file editing |
| **OpenCode** | JSON (`opencode.jsonc`)| Yes | Context-first systems counseling & multi-model routing |
| **Aider** | — | Yes | Surgical git diffs & local file edits |

> ℹ️ **Family Isolation**: Gemini CLI and `agy` share the `gemini-antigravity` family. When either is the human-facing orchestrator, both are excluded from automatic worker selection to protect orchestrator bandwidth.

---

## 🎯 Orchestration & Capability Dispatch

Policy configuration is located at `~/.mindsync/orchestration.json` (Modes: `auto`, `suggest`, `off`).

### Run Dispatched Jobs
```bash
# Route task automatically to best suited agent by capability
mindsync-dispatch run auto "implement and test the auth fix" --capability coding

# Check status of running or completed jobs
mindsync-dispatch status
```

### Reactive Provider Cooldowns
When a provider reports quota exhaustion, MindSync marks it cooling down so routing skips it temporarily. `--on-limit` defaults to `stop`; the open core does not transfer work to another worker.

```bash
# Inspect provider cooldowns
mindsync-dispatch limits

# Clear cooldowns manually after operator verification
mindsync-dispatch limits clear
```

### MindSync Pro
MindSync Pro is coming soon. The following capabilities are reserved for the private platform:

- Worker quota handoff with `--on-limit handoff`, successor transfer, and handoff checkpoints.
- Pre-emptive provider usage evaluation and usage readers.
- Codex standalone reserve warnings.
- Automated pull requests on completion.
- Remote sync, including the `submit` and `worker` CLI commands.

---

## 🧠 Persistent Memory & Shared Facts

MindSync embeds a lightweight, local vector and relational database powered by `sqlite-vec` for cross-session knowledge retention:

```bash
# View memory database statistics
mindsync memory stats

# List recorded facts for a specific repository
mindsync memory list --project my-repo

# Semantic search across historical decisions and architecture facts
mindsync memory recall --project my-repo --query "database migration decision"
```

### MCP Tool Bundles
* **Orchestrator Hosts (16 Tools)**: Exposes full orchestration (`delegate_task`, `route_task`, `get_sync_context`, `update_focus`, `memory_checkpoint`, `memory_recall`, `queue_durable_fact`, etc.).
* **Dispatched Workers (12 Tools)**: Runs with `MINDSYNC_WORKER=1`, omitting recursive delegation tools while retaining shared context, focus locks, and fact retrieval.

---

## 🔒 Safety & Security Architecture

1. **Human-in-the-Loop Authority**: The human-facing orchestrator CLI always retains final approval and verification.
2. **Explicit Binary Execution**: `mindsync setup` only configures known recipes. Unknown binaries are suggested, never executed blindly.
3. **Crash-Safe Locking**: State updates use atomic writes and file locks. On Windows, lock contention timeouts are configurable via environment variables.
4. **Secret-Safe Serialization**: Job status and telemetry strictly strip access tokens, auth headers, and raw credential structures before logging.

---

## ⚙️ Advanced Configuration & Environment Variables

<details>
<summary><b>Click to expand Environment Variables</b></summary>

| Variable | Description | Default |
| :--- | :--- | :--- |
| `MINDSYNC_HOME` | Root configuration directory | `~/.mindsync` |
| `AGENT_DISPATCH_HOME` | Storage for dispatch jobs and rosters | `~/.mindsync/dispatch` |
| `MINDSYNC_CALLER_CLI` | Declares calling CLI engine identity | Auto-detected |
| `MINDSYNC_QUEUE_LOCK_TIMEOUT` | Max wait time for file locks (seconds) | `10.0` |
| `MINDSYNC_LOCK_CONTENTION_BACKOFF_BASE` | Backoff base for Windows lock contention | `0.05` |

</details>

---

## 🛠️ Development & Testing

```bash
# 1. Clone repository
git clone https://github.com/adityarya24/mindsync-ai.git
cd mindsync-ai

# 2. Set up virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 3. Install in editable mode with development dependencies
python -m pip install -e ".[dev]"

# 4. Run linter & test suite
python -m ruff check .
python -m pytest -q
```

---

## 📄 License

Distributed under the [MIT License](LICENSE). Open-source and free for personal and commercial use.
