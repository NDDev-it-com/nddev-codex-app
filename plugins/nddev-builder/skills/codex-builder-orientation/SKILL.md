---
name: codex-builder-orientation
description: Start here for the nddev-builder toolkit. Indexes the Codex artifact families and the creator, checker, reviewer, and workflow skill that owns each, points at the single reference for schemas and current (Beta, experimental, or under-development) surfaces, and routes the task to the right skill. Use first whenever building, checking, reviewing, or releasing a Codex artifact, or when unsure which builder skill applies.
---

# Codex Builder Orientation

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

The first skill to read for any nddev-builder task: index the artifact families, point at the schema and currency source of truth, and route to the skill that owns the work.

New to the toolkit? Start with `$codex-onboarding` — a guided first run that walks a newcomer from zero to a checked artifact through the paths below.

## Workflow

1. Name the artifact family the task targets. The generator and checker own the authoritative family list; read it rather than trusting any copy:

       python3 ../../scripts/create_codex_artifact.py --help
       python3 ../../scripts/check_codex_artifact.py all .

2. Read ../../references/codex-artifact-contracts.md for that family's canonical surface, minimum contract, and cross-artifact invariants, and its "Current moving surfaces" section for what is Beta, experimental, or under development. For live runtime currency run `codex --version` and consult the built-in `$openai-docs` skill; never freeze a version into this page.

3. Route to the owning pair by naming convention: family `<family>` is authored by `$codex-<family>-creator` and validated by `$codex-<family>-checker` (for example `$codex-skill-creator` and `$codex-skill-checker`). Invoke the one that matches; let its own description carry the detail.

4. For cross-cutting work, route to a workflow skill instead of a single family: `$codex-artifact-reviewer` for a cross-artifact release-readiness audit; `$codex-plugin-scaffolder`, `$codex-plugin-devtest`, and `$codex-plugin-publish` for the plugin build, test, and publish lifecycle; and `$codex-release-review` for release review.

5. Orient on the ownership boundaries the contracts reference details before authoring: custom subagents are standalone config-scope TOML, not a plugin-bundled component; Codex has no first-class authored slash commands, so reusable command workflows become skills; and a plugin manifest bundles only the capabilities that reference enumerates (skills, hooks, MCP servers, apps).

## Quality bar

- Read this first for any create, check, review, or release task; confirm the family and its current surface before opening a creator.
- Route to exactly one owning skill and defer to its description; do not duplicate another skill's workflow on this page.
- Treat the contracts reference and the script `--help` as the only source of families, schemas, and currency; this page names owners, it never freezes their contents.
- A static generate or check proves shape only; runtime discovery, trust, and behavior stay with the matching skill's runtime step.
