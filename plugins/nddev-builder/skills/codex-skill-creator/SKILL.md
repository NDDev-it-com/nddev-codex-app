---
name: codex-skill-creator
description: Create or revise native Codex skills with precise routing, progressive disclosure, reusable resources, and OpenAI UI metadata. Use when adding a SKILL.md workflow or improving an existing skill.
---

# Codex Skill Creator

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Create one focused Codex skill that is easy to trigger, cheap to load, and deterministic where automation helps.

## Workflow

1. Identify the skill's single job, trigger phrases, output, and repository or user scope.
2. Prefer the built-in $skill-creator workflow for discovery and first-party scaffolding.
3. Read ../../references/codex-artifact-contracts.md before deciding paths or metadata.
4. If a scaffold is still needed, inspect options and run:

       python3 ../../scripts/create_codex_artifact.py skill --help
       python3 ../../scripts/create_codex_artifact.py skill --output .agents/skills --name example-skill --description "Create repeatable repository workflows"

5. Replace generated examples with concise instructions. Keep exact schemas or long guidance in references/, deterministic work in scripts/, and output assets in assets/.
6. Add agents/openai.yaml with a useful display name, a 25-64 character summary, and a default prompt that explicitly names the skill.
7. Do not create slash-command files; Codex workflows are skills.
8. Validate the finished directory:

       python3 ../../scripts/check_codex_artifact.py skill .agents/skills/example-skill

9. Exercise at least one realistic explicit invocation and one description-based routing case. Report files changed and checks run.

## Quality bar

- The directory name and frontmatter name match and use lowercase hyphen-case.
- SKILL.md frontmatter contains only name and a trigger-rich description.
- Instructions state decisions and checks, not generic encouragement.
- Resources are referenced from SKILL.md and contain no secrets, caches, or generated runtime state.
