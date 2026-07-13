---
name: codex-rule-creator
description: Create or revise native Codex execpolicy .rules files with narrow command prefixes, explicit decisions, and executable positive and negative examples. Use when governing shell-command approval behavior.
---

# Codex Rule Creator

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Create a minimal execpolicy rule that classifies one command family without granting unintended variants.

## Workflow

1. Define the exact command tokens, desired decision, trust rationale, platforms, and both allowed and disallowed examples.
2. Read ../../references/codex-artifact-contracts.md for the current rules language, locations, and evaluator behavior.
3. Inspect options and generate:

       python3 ../../scripts/create_codex_artifact.py rule --help
       python3 ../../scripts/create_codex_artifact.py rule --output .codex/rules --name safe-shell --description "Execpolicy for a bounded command family" --prefix git status

4. Use token-aware prefixes, not shell substrings. Prefer multiple narrow rules over one broad pattern.
5. Include justification and match examples where supported. Treat shell wrappers, flags, paths, and subcommands as separate attack surfaces.
6. Do not encode secrets, environment-specific home paths, or blanket allow rules.
7. Validate:

       python3 ../../scripts/check_codex_artifact.py rule .codex/rules/safe-shell.rules

8. Run the native rules evaluator against positive, negative, wrapper, injection, and near-miss cases when available.

## Quality bar

- Every allow decision is bounded to the intended argv shape.
- Negative examples cover sibling subcommands, added shell operators, wrappers, and path escapes.
- Overlap between rules has a deliberate outcome.
- The file is portable, documented, and fails closed on ambiguous input.
