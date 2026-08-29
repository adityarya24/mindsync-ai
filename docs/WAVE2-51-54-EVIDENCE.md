# Wave 2 evidence: issues #51 and #54

Date: 2026-08-29  
Base: `6adb1f9` (master after #55/#56/#57)  
Scope: recorded wrap-up verdict. No new readers. Claude reset parser already shipped.

Hard boundaries observed: no browser profiles, cookie databases, keychains, or
pasted live tokens. MindSync sees CLI stdout/stderr, not provider HTTP headers.

## #51 usage-reader matrix

| Adapter | Upstream identity | Authoritative usage source MindSync can see | Credential class | Usable fields | Verdict |
|---|---|---|---|---|---|
| `codex` | OpenAI / ChatGPT account (`quotaScope` `openai:default`, reader `codex-oauth`) | Local `~/.codex/auth.json` OAuth + `chatgpt.com/backend-api/wham/usage` (already implemented) | Operator-local Codex OAuth file, revocable via `codex logout` | `used_percent`, optional `reset_at` | **implementable** — already in tree |
| `claude` | Anthropic (`anthropic:default`) | No in-repo CLI usage endpoint. Exhaustion is stderr-classified (`quotaErrorPatterns`). Anthropic API rate-limit *headers* are not visible to MindSync. | Browser/session cookies or undocumented CLI internals would be required for a live % reader | n/a | **reactive-only** |
| `agy` / `gemini` | Shared `quotaScope` `google:default` — two CLIs, one Google account | No sanitized Gemini CLI usage JSON in-repo. Google AI Studio / Cloud quota APIs need a Cloud credential, not the Gemini CLI login cookie. | Browser cookie import is forbidden; ADC/API keys are a different identity than Antigravity CLI login | n/a | **reactive-only** until an official CLI usage command exists |
| `grok` | xAI (preset has no `quotaScope`; degrades to `agent:grok`) | `GROK_API_KEY` is a generic key, not a usage document. No CLI `usage` subcommand in the preset. | API key in env is not a documented usage-percentage contract with a denominator | n/a | **reactive-only** |
| `cursor` | Cursor account (not the model vendor) | Cursor CLI (`cursor-agent`) has no documented usage dump in this repo. Dashboard usage is a browser session. | Cookie/DB harvest forbidden | n/a | **blocked pending explicit security approval** for any dashboard scrape |
| `opencode` | **Adapter ≠ account.** OpenCode can route many upstreams | No single usage endpoint. A reader keyed to `opencode` would mix providers. | Would need per-upstream credentials, not OpenCode login | n/a | **blocked pending explicit security approval** (identity split) |
| `aider` | **Adapter ≠ account.** Model chosen by `--model` / env keys | No Aider usage API. Local token counters are not an authoritative denominator | Env API keys are not usage documents | n/a | **blocked pending explicit security approval** (identity split) |

### #51 conclusion

**Closed as recorded verdict (2026-08-29).** Codex is the only implementable
CLI-token reader. Other adapters stay `reactive-only` or blocked. MindSync will
not harvest browser cookies or keychains. An operator-supplied credential path
is a future product decision, not an open implementation checklist.

Doctor reports `usage_mode` and `reactive_reset` per adapter.

## #54 reactive reset formats

| Adapter | CLI prints authoritative reset on exhaustion? | Evidence | Parser |
|---|---|---|---|
| `claude` | Yes — `Claude AI usage limit reached\|<10-digit unix seconds>` | In-repo classifier fixture + Wave 1 parser | **shipped** (`limits.extract_reactive_reset_at`) |
| `codex` | Pattern mentions “reset/try again” but no allowlisted timestamp fixture | Preset regex only | **fallback-only** (`quotaCooldownSeconds`) |
| `agy` / `gemini` | No sanitized exhaustion stderr in-repo | HTTP retry headers ≠ CLI stderr | **fallback-only** |
| `grok` | No sanitized fixture | — | **fallback-only** |
| `cursor` | No sanitized fixture | — | **fallback-only** |
| `opencode` | No sanitized fixture | — | **fallback-only** |
| `aider` | No sanitized fixture | — | **fallback-only** |

`quotaCooldownSeconds` remains an operator-set estimate, not provider truth.

### #54 conclusion

**Closed as recorded verdict (2026-08-29).** Claude's 10-digit stderr epoch is
the only allowlisted reactive reset parser. Every other adapter keeps
`quotaCooldownSeconds` as an operator-set estimate, not provider truth. New
parsers wait on a sanitized CLI exhaustion fixture — they are not guessed.
