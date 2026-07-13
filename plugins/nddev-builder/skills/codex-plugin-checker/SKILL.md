---
name: codex-plugin-checker
description: Check a native Codex plugin manifest, bundled capability paths, policies, and nested artifacts. Use before marketplace publication, installation, version bumps, or plugin releases.
---

# Codex Plugin Checker

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Verify the plugin as a distributable boundary, not just as a parseable manifest.

## Workflow

1. Read ../../references/codex-artifact-contracts.md and inspect the full plugin tree.
2. Run:

       python3 ../../scripts/check_codex_artifact.py plugin plugins/example-plugin

3. Confirm .codex-plugin/plugin.json exists at the root and its identity, version, and declared paths agree with disk.
4. Check every bundled skill, app, MCP server, and hook with its artifact-specific contract.
5. Reject copied slash-command surfaces and plugin-bundled custom agent TOML.
6. Review authentication, installation, environment variables, executable paths, and network needs for clear ownership and no embedded secrets.
7. In an isolated CODEX_HOME, test marketplace installation and plugin discovery when the runtime supports them.

## Result

Return PASS only when schema, nested artifacts, and available runtime checks succeed. Otherwise return FAIL with file-specific findings ordered by severity.
