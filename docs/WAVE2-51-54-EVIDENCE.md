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
| `claude` | Anthropic (`anthropic:default`, reader `claude-oauth`) | Local `~/.claude/.credentials.json` OAuth + `api.anthropic.com/api/oauth/usage` | Operator-local Claude Code OAuth file, revocable via `claude auth logout` | session + weekly `used_percent`, optional `reset_at` | **implementable** |
| `agy` / `gemini` | Shared `quotaScope` `google:default`, reader `antigravity-oauth` | Official Antigravity CLI vault (`gemini:antigravity`) + `retrieveUserQuotaSummary` | CLI OAuth store, not Chrome cookies | 5h + weekly remaining fractions | **implementable** |
| `grok` | xAI (`xai:default`, reader `grok-oauth`) | Local `~/.grok/auth.json` session + `cli-chat-proxy.grok.com/v1/billing?format=credits` | Operator-local Grok CLI OAuth file | weekly `creditUsagePercent`, optional product window | **implementable** |
| `cursor` | Cursor account (`cursor:default`, reader `cursor-oauth`) | Local Cursor IDE `state.vscdb` `cursorAuth/accessToken` + `GetCurrentPeriodUsage` | IDE session DB, read-only, **opt-in** via `usage.readers.cursor` | plan `autoPercentUsed` / `totalPercentUsed` | **implementable**, default off |
| `opencode` | OpenCode Go plan (`opencode-go:default`, reader `opencode-go`) | Local `opencode-go` API key + `opencode.ai/zen/go/v1/usage` | Go subscription key only — not BYOK upstreams | rolling + weekly/monthly percent | **implementable** for Go; other OpenCode upstreams stay out of this reader |
| `aider` | **Adapter ≠ account.** Model chosen by `--model` / env keys | No Aider usage API. Local token counters are not an authoritative denominator | Env API keys are not usage documents | n/a | **blocked pending explicit security approval** (identity split) |

### #51 conclusion

**Updated 2026-08-30.** Codex remains the reference reader. Claude, Grok,
Antigravity, Cursor, and OpenCode Go now have native readers using local CLI/IDE
session stores (not Chrome cookie import). OpenCode BYOK upstreams stay out of
the Go reader. Browser profiles and keychains other than the official
Antigravity CLI vault are still forbidden.

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
