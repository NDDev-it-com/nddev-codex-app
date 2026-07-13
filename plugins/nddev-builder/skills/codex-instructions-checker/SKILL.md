---
name: codex-instructions-checker
description: Check AGENTS.md files for discovery, scope, precedence, contradictions, stale facts, executable commands, and native Codex boundaries. Use before merging instruction changes or diagnosing agent behavior.
---

# Codex Instructions Checker

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Audit the effective instruction chain and distinguish structural validity from semantic correctness.

## Workflow

1. Locate the target file and every applicable parent or child instruction file. Read ../../references/codex-artifact-contracts.md.
2. Run:

       python3 ../../scripts/check_codex_artifact.py instructions AGENTS.md

3. Verify placement, filename, size, headings, referenced paths, and runnable commands.
4. Compare claims with code, config, manifests, and current repository state. Flag duplicated machine-owned values.
5. Review precedence for contradictions, accidental scope expansion, unsafe authority, and rules that belong in a skill or validator.
6. Reject copied slash-command models, collapsed foreign-CLI instruction files, secrets, speculation, and runtime-local state.
7. If diagnosing behavior, start an isolated Codex session from representative directories and inspect the discovered chain.

## Result

Return PASS only when structure, facts, and precedence are sound. Otherwise return FAIL with file and heading, conflicting evidence, scope impact, and correction.
