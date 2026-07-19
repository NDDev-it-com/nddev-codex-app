---
name: codex-requirements-creator
description: Create a managed Codex requirements.toml layer that constrains permission profiles and other admin policy. Use when authoring the /etc/codex or MDM-delivered managed requirements a lower config layer must satisfy.
---

# Codex Requirements Creator

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Create the smallest managed requirements layer that expresses the intended administrative constraint and still leaves a valid default for lower layers.

## Workflow

1. Identify the managed delivery channel (system `/etc/codex/requirements.toml`, Windows ProgramData, or macOS MDM `com.openai.codex`) and the exact policy being constrained.
2. Read ../../references/codex-artifact-contracts.md for the managed requirements surface and the allowed permission profiles.
3. Inspect options and generate a baseline:

       python3 ../../scripts/create_codex_artifact.py requirements --help
       python3 ../../scripts/create_codex_artifact.py requirements --output managed --name managed --description "Managed Codex requirements" --allowed-permission-profile :read-only --allowed-permission-profile :workspace

4. Constrain only what the policy requires. Keep a valid default: when `default_permissions` is unset, permit both `:read-only` and `:workspace`; otherwise make the default one of the allowed profiles.
5. Reference only built-in profiles or profiles the same file defines under `[permissions.<name>]`. Keep secrets and machine-local state out of the file.
6. Validate:

       python3 ../../scripts/check_codex_artifact.py requirements managed/requirements.toml

7. Apply the file to a temporary managed layer with isolated HOME and CODEX_HOME and confirm the effective allowed-profile set without mutating live state.

## Quality bar

- The constraint is intentional, minimal, and internally consistent.
- A lower layer always retains a valid selectable default.
- Every referenced profile is a known built-in or a defined `[permissions.<name>]`.
- Unknown, deprecated, mixed-sandbox, and secret-bearing keys are absent.
