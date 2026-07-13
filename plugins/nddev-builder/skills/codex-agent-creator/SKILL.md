---
name: codex-agent-creator
description: Create or revise native Codex custom subagent TOML with a narrow role, clear delegation contract, and intentional runtime controls. Use when adding repository or user agents for specialized parallel work.
---

# Codex Agent Creator

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Create a standalone custom agent whose scope and authority are obvious to both the parent agent and reviewers.

## Workflow

1. Define the role, delegation triggers, expected report, read/write scope, and risks.
2. Read ../../references/codex-artifact-contracts.md for current locations and supported TOML keys.
3. Inspect options and generate a starting file:

       python3 ../../scripts/create_codex_artifact.py agent --help
       python3 ../../scripts/create_codex_artifact.py agent --output .codex/agents --name reviewer --description "Review bounded changes and report evidence"

4. Write a unique name, a delegation-oriented description, and self-contained developer instructions.
5. Select model, reasoning, sandbox, skills, and MCP overrides only when the role needs them; otherwise inherit stable defaults.
6. Keep agents under repository .codex/agents/ or CODEX_HOME/agents/. Custom agents are standalone TOML and are not bundled inside a plugin.
7. Validate:

       python3 ../../scripts/check_codex_artifact.py agent .codex/agents/reviewer.toml

8. Delegate one bounded sample task and confirm the report is concise, evidence-backed, and within scope.

## Quality bar

- The role is materially narrower than the parent agent.
- Instructions specify task, constraints, output, authority, and stop conditions.
- Tool access is least-privilege for the work.
- No secrets, local absolute paths, synthetic MCP servers, or unsupported config keys are embedded.
