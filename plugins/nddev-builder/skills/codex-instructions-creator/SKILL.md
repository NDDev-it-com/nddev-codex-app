---
name: codex-instructions-creator
description: Create or revise layered AGENTS.md instructions with clear scope, verified repository facts, native Codex boundaries, and concise operating rules. Use when adding project, directory, or global agent guidance.
---

# Codex Instructions Creator

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Write the narrowest instruction layer that helps Codex act correctly in its directory scope without duplicating lower-level truth.

## Workflow

1. Determine the file's scope, parent instruction chain, target audience, and facts that genuinely need durable guidance.
2. Read ../../references/codex-artifact-contracts.md for discovery, precedence, fallback, and size rules.
3. Inspect options and generate:

       python3 ../../scripts/create_codex_artifact.py instructions --help
       python3 ../../scripts/create_codex_artifact.py instructions --output . --name repository-instructions --description "Repository instructions for Codex"

4. Replace scaffold text with verified purpose, boundaries, workflows, safety constraints, and exact verification commands.
5. Keep code and config as source of truth; reference canonical files instead of copying volatile versions or pins.
6. Use skills and plugins for detailed workflows. Do not create slash-command instructions or collapse another CLI's first-class file into an import.
7. Validate:

       python3 ../../scripts/check_codex_artifact.py instructions AGENTS.md

8. Review the full parent-to-child chain for contradictions, duplication, and stale paths.

## Quality bar

- Every rule is actionable, scoped, and based on repository truth.
- More specific files refine parent guidance without silently reversing safety boundaries.
- Commands are executable from the stated directory.
- No secrets, chat transcripts, speculation, or runtime-local state are stored.
