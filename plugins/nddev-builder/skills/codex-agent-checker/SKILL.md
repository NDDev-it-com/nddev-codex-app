---
name: codex-agent-checker
description: Check native Codex custom-agent TOML, role clarity, supported overrides, authority, and delegation behavior. Use before installing an agent or after changing its instructions, tools, model, or sandbox.
---

# Codex Agent Checker

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Validate both the TOML contract and whether the agent can be delegated safely and predictably.

## Workflow

1. Read ../../references/codex-artifact-contracts.md and locate the standalone agent TOML.
2. Run:

       python3 ../../scripts/check_codex_artifact.py agent .codex/agents/reviewer.toml

3. Confirm required identity and instruction fields are present, supported, and internally consistent.
4. Review the description as a routing contract and the developer instructions as a self-contained work envelope.
5. Check model, reasoning, sandbox, skills, and MCP overrides for necessity and least privilege.
6. Reject plugin-bundled custom agents, synthetic inherited MCP declarations, secrets, and machine-specific absolute paths.
7. Delegate a representative bounded task when a Codex runtime is available; verify scope, evidence, and report format.

## Result

Return PASS only when structural and delegation checks succeed. Otherwise return FAIL with the TOML path, key or instruction finding, risk, and correction.
