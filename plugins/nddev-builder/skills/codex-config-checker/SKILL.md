---
name: codex-config-checker
description: Check Codex config.toml syntax, supported keys, precedence, safety-model consistency, paths, MCP transports, and runtime parsing. Use before installing or changing any Codex configuration layer.
---

# Codex Config Checker

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Validate the file in its intended scope and inspect the effective trust posture, not merely TOML syntax.

## Workflow

1. Identify user, repository, managed, or setup scope and read ../../references/codex-artifact-contracts.md.
2. Run:

       python3 ../../scripts/check_codex_artifact.py config .codex/config.toml

3. Check supported keys, types, duplicate ownership, deprecated aliases, profile selectors, feature flags, paths, and MCP definitions.
4. Fail any active layer that mixes permission-profile settings with legacy sandbox settings.
5. Review approval, sandbox, filesystem, network, domain, and workspace settings against the stated trust boundary.
6. Reject secrets, user-specific runtime state, incomplete transports, and non-portable absolute paths.
7. Parse and start Codex with temporary HOME and CODEX_HOME; inspect effective config or diagnostics when supported.

## Result

Return PASS only when static consistency and available runtime parsing succeed. Otherwise return FAIL with table/key path, effective risk, and correction.
