---
name: codex-requirements-checker
description: Check a managed Codex requirements.toml layer for supported keys, permission-profile consistency, and a valid lower-layer default. Use before delivering managed /etc/codex or MDM requirements.
---

# Codex Requirements Checker

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Validate the managed requirements layer in its delivery scope and inspect the effective constraint it imposes, not merely TOML syntax.

## Workflow

1. Identify the managed delivery channel (system `/etc/codex/requirements.toml`, Windows ProgramData, or macOS MDM `com.openai.codex`) and read ../../references/codex-artifact-contracts.md.
2. Run:

       python3 ../../scripts/check_codex_artifact.py requirements managed/requirements.toml

3. Check supported top-level keys, the `allowed_permission_profiles` table shape, and that every referenced profile is a built-in or defined under `[permissions.<name>]`.
4. Confirm a valid default remains: `default_permissions` must be one of the allowed profiles, or, when unset, the allowed set must permit both `:read-only` and `:workspace`.
5. Fail any layer that mixes permission-profile settings with legacy sandbox settings, or that carries deprecated or secret-bearing keys.
6. Apply the file to a temporary managed layer with isolated HOME and CODEX_HOME; confirm the effective allowed-profile set or diagnostics.

## Result

Return PASS only when static consistency and available runtime confirmation succeed. Otherwise return FAIL with the key path, the effective constraint risk, and the correction.
