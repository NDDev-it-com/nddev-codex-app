---
name: codex-mcp-checker
description: Check Codex MCP JSON or TOML definitions, transport completeness, launcher provenance, environment handling, and runtime connectivity. Use before enabling, bundling, or releasing an MCP server.
---

# Codex MCP Checker

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Validate the source definition, then prove the declared transport can initialize without leaking live credentials.

## Workflow

1. Identify whether the definition is plugin .mcp.json or Codex config TOML and read ../../references/codex-artifact-contracts.md.
2. For a plugin definition, run:

       python3 ../../scripts/check_codex_artifact.py mcp .mcp.json

3. Check stable unique names, required transport fields, explicit argv, valid cwd and paths, pinned mutable launchers, and complete disabled-server metadata.
4. Reject inline tokens, secrets, synthetic inherited servers, ambiguous shell strings, and missing authentication ownership.
5. Use a temporary HOME and CODEX_HOME with stub credentials. Start stdio servers or perform HTTP initialization, list tools, and exercise one safe read-only call when available.
6. Confirm failures are actionable and do not hang indefinitely.

## Result

Return PASS only when schema, security review, and available connectivity checks succeed. Otherwise return FAIL with server name, field or runtime evidence, and correction.
