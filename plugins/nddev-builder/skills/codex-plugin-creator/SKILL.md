---
name: codex-plugin-creator
description: Create or revise native Codex plugins with valid manifests and intentionally bundled skills, apps, MCP servers, or hooks. Use when building a distributable Codex plugin rather than standalone files.
---

# Codex Plugin Creator

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Build a minimal plugin whose manifest and bundled capabilities form one coherent product boundary.

## Workflow

1. Define the plugin's purpose, installation scope, bundled capabilities, and authentication requirements.
2. Prefer the built-in $plugin-creator for first-party scaffolding and manifest guidance.
3. Read ../../references/codex-artifact-contracts.md before selecting directories or manifest fields.
4. If a scaffold is still needed, inspect options and run:

       python3 ../../scripts/create_codex_artifact.py plugin --help
       python3 ../../scripts/create_codex_artifact.py plugin --output plugins --name example-plugin --description "Bundle focused Codex development workflows"

5. Keep .codex-plugin/plugin.json at the plugin root. Add only capabilities the plugin actually owns.
6. Use native skills for workflows; do not copy Claude or OpenCode slash commands.
7. Keep custom agents as standalone TOML under .codex/agents/ or CODEX_HOME/agents/, not inside the plugin bundle.
8. Validate the complete plugin:

       python3 ../../scripts/check_codex_artifact.py plugin plugins/example-plugin

9. If Codex is available, install from an isolated marketplace and verify discovery without touching live user state.

## Quality bar

- Manifest identity, version, and capability paths agree with the filesystem.
- Authentication and installation policy are explicit and least-surprising.
- Bundled files are portable, contain no secrets, and use relative paths safely.
- Every bundled skill, app, MCP server, and hook passes its own checker.
