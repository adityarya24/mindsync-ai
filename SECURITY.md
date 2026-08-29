# Security Policy

## Supported Versions

Currently, the following versions are supported with security updates:

| Version | Supported          |
| ------- | ------------------ |
| 1.7.x   | :white_check_mark: |
| 1.6.x   | :white_check_mark: |
| < 1.6   | :x:                |

## Trust Boundaries and Threat Model

MindSync operates under the following trust model:

1. **Local Domain (`MINDSYNC_HOME`)**: A single MindSync instance and its `MINDSYNC_HOME` directory constitute a **single trust domain**. There is no isolation between different projects running under the same home. For confidential or unrelated projects, use separate `MINDSYNC_HOME` directories.
2. **Durable Facts**: Facts queued by LLM agents are treated as **untrusted data**. MindSync validates fields to prevent path traversal (e.g., rejecting paths in entity identifiers) and sanitizes identifiers, but the text payload is free-form. Compiled truth rendered by the remote host should be treated as data, not executable agent instructions.
3. **Remote SSH Execution**: The SSH remote connection assumes that the target host is trusted and that the \`MINDSYNC_REMOTE_ENV_FILE\` is safe to source. 
4. **Agent Dispatch**: Dispatch runs configured CLI agents and optional review commands with the privileges of the MindSync user. Adapter configuration, prompts, working directories, and check commands must therefore come only from trusted local clients. Worktree isolation prevents accidental overlap; it is not a security sandbox and cannot stop an agent from accessing paths outside its worktree.

## Reporting a Vulnerability

Please do not open a public issue for security vulnerabilities. Instead, responsibly disclose it by emailing the maintainer directly or using GitHub's private vulnerability reporting feature on this repository.

We will acknowledge receipt within 48 hours and work with you to triage and resolve the issue.
