---
name: codex-rule-checker
description: Check native Codex execpolicy .rules syntax, command-prefix scope, overlap, bypasses, and positive and negative examples. Use before installing rules or changing shell-command approval policy.
---

# Codex Rule Checker

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Treat every allow rule as a security boundary and try to match more commands than its author intended.

## Workflow

1. Read ../../references/codex-artifact-contracts.md and inventory all rules that may overlap the target file.
2. Run:

       python3 ../../scripts/check_codex_artifact.py rule .codex/rules/safe-shell.rules

3. Check supported syntax, decisions, token patterns, justifications, examples, duplicates, and precedence.
4. Build cases for intended commands, sibling subcommands, alternate flags, shell wrappers, operators, path traversal, executable aliases, and extra trailing tokens.
5. Run the native rules evaluator when available and compare every actual decision with the documented expectation.
6. Reject blanket allows, substring reasoning, secrets, user-specific paths, and rules whose ambiguity broadens authority.

## Result

Return PASS only when syntax and adversarial cases match the intended policy. Otherwise return FAIL with rule location, reproducing argv, actual decision, expected decision, and severity.
