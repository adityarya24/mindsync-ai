# Sample remote backend

MindSync does **not** ship a full remote database. The MCP server only expects
three hooks on the machine pointed at by `MINDSYNC_SSH_HOST` + `MINDSYNC_REMOTE_ROOT`:

| Hook | Default path | Contract |
| --- | --- | --- |
| Env file (optional) | `config/mindsync.env` | Sourced before commands if present |
| Write fact | `tools/mindsync_fact.py` | CLI: `write --agent --entity --attribute --text --source --confidence` |
| Consolidate | `tools/mindsync_consolidate.py` | Rebuilds markdown under `compiled-truth/` |
| Truth dir | `compiled-truth/` | Directory of `*.md` summaries pulled via SCP |

Copy these stubs onto your server, replace the storage body with Postgres/SQLite/files,
and set:

```bash
export MINDSYNC_SSH_HOST=my-server
export MINDSYNC_REMOTE_ROOT=/opt/mindsync
```

SSH must allow non-interactive (`BatchMode`) auth from the developer machine.
