---
name: codex-hook-checker
description: Check Codex hooks.json schemas, supported events, command references, timeouts, matchers, and failure behavior. Use before trusting, installing, or changing plugin-bundled lifecycle hooks.
---

# Codex Hook Checker

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Verify manifest structure and execute handlers as hostile boundaries with controlled inputs.

## Workflow

1. Read ../../references/codex-artifact-contracts.md and inventory all referenced handlers.
2. Run:

       python3 ../../scripts/check_codex_artifact.py hook hooks/hooks.json

3. Check supported event names, matcher scope, command type, positive bounded timeouts, status messages, and duplicate lifecycle ownership.
4. Resolve PLUGIN_ROOT references and fail missing, non-portable, unquoted, or escaping paths.
5. Inspect handlers for secrets, unsafe shell interpolation, swallowed failures, unexpected network use, and writes outside intended roots.
6. Execute valid, malformed, failing, and timeout cases in a temporary directory without live credentials.
7. When available, compare discovery through hooks/list with the checked manifest.

## Result

Return PASS only when static and isolated execution checks succeed. Otherwise return FAIL with event, handler path, reproduction, and impact.
