---
name: codex-app-creator
description: Create or revise native Codex .app.json mappings with stable ChatGPT developer-mode app ids, plugin linkage, and clear authentication ownership. Use when a plugin exposes an app-backed connector to Codex.
---

# Codex App Creator

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Map only the apps the plugin truly exposes, without duplicating credentials or MCP transport data.

## Workflow

1. Inventory app names, canonical developer-mode ids, plugin linkage, and the user-visible authentication path.
2. Read ../../references/codex-artifact-contracts.md for the current .app.json schema.
3. Inspect kind-specific options, then generate:

       python3 ../../scripts/create_codex_artifact.py app --help
       python3 ../../scripts/create_codex_artifact.py app --output . --name example-app --description "Connector declarations for the example plugin" --app-id plugin_asdk_app_Example123

4. Replace example ids with source-backed ChatGPT developer-mode app ids and make the plugin manifest point to the actual .app.json.
5. Keep authentication outside the manifest; never add tokens, cookies, or user-specific state.
6. Validate:

       python3 ../../scripts/check_codex_artifact.py app .app.json

7. Install in an isolated supported ChatGPT task and exercise the connected app when credentials are available.

## Quality bar

- App keys are stable, unique, and map to canonical developer-mode ids.
- The plugin manifest links to the actual app mapping.
- The app manifest owns declarations only; transport belongs to MCP and credentials belong to the auth flow.
- No undocumented fields or live account data are present.
