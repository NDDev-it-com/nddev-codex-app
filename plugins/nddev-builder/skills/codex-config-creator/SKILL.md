---
name: codex-config-creator
description: Create or revise Codex config.toml layers with explicit model, safety, feature, project, and MCP settings. Use when adding a repository config, CODEX_HOME config, or setup overlay.
---

# Codex Config Creator

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Create the smallest configuration layer that owns the requested behavior and composes cleanly with higher-precedence scopes.

## Workflow

1. Identify scope, precedence, trust boundary, portability needs, and the exact settings being changed.
2. Read ../../references/codex-artifact-contracts.md for supported keys and current deprecations.
3. Inspect options and generate a baseline:

       python3 ../../scripts/create_codex_artifact.py config --help
       python3 ../../scripts/create_codex_artifact.py config --output .codex --name config --description "Repository Codex configuration"

4. Add only source-backed keys. Keep user credentials and machine-local state outside the file.
5. Do not mix permission-profile settings with legacy sandbox settings in one active layer. Avoid deprecated aliases and legacy profile selectors.
6. Keep MCP runtime definitions complete and use environment references for secrets.
7. Validate:

       python3 ../../scripts/check_codex_artifact.py config .codex/config.toml

8. Run Codex with temporary HOME and CODEX_HOME to confirm parsing and effective behavior without mutating live state.

## Quality bar

- Scope and precedence are intentional and documented.
- Safety posture is internally consistent and no broader than requested.
- All paths and launchers are portable or explicitly environment-provided.
- Unknown, deprecated, duplicate, and secret-bearing keys are absent.
