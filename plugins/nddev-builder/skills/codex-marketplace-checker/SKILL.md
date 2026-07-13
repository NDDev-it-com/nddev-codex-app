---
name: codex-marketplace-checker
description: Check Codex marketplace JSON, plugin identities and sources, categories, policies, and reachable local plugins. Use before marketplace registration, publication, or catalog updates.
---

# Codex Marketplace Checker

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Validate catalog syntax, source integrity, and whether each entry can be installed as described.

## Workflow

1. Read ../../references/codex-artifact-contracts.md and identify the marketplace root.
2. Run:

       python3 ../../scripts/check_codex_artifact.py marketplace .agents/plugins/marketplace.json

3. Check unique plugin ids, supported source forms, safe relative paths, categories, and required installation and authentication policy.
4. Resolve every local source and run the plugin checker on its complete plugin root.
5. Flag mutable or ambiguous remote provenance, embedded credentials, duplicate source-of-truth values, and entries that escape the catalog boundary.
6. In an isolated CODEX_HOME, exercise marketplace add/list and plugin discovery when those runtime methods are available.

## Result

Return PASS only when the catalog and reachable local plugins pass. Otherwise return FAIL with the marketplace entry, source path, and actionable reason.
