---
name: codex-app-checker
description: Check native Codex .app.json names, developer-mode app ids, plugin linkage, authentication boundaries, and discovery. Use before bundling, installing, or changing app mappings.
---

# Codex App Checker

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Verify that every app mapping is source-backed, linked, and safe to distribute.

## Workflow

1. Read ../../references/codex-artifact-contracts.md and locate the plugin-root .app.json.
2. Run:

       python3 ../../scripts/check_codex_artifact.py app .app.json

3. Check unique app keys, source-backed developer-mode ids, conservative fields, and absence of placeholder values.
4. Confirm the plugin manifest points to the actual .app.json and do not infer undocumented standalone schema requirements.
5. Reject tokens, cookies, account identifiers, MCP transport duplication, and machine-local state.
6. Install in an isolated supported ChatGPT task and exercise the connected app when credentials are available.

## Result

Return PASS only when the mapping, linkage, and available runtime checks succeed. Otherwise return FAIL with app key, field, impact, and correction.
