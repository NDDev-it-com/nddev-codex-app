---
name: codex-skill-checker
description: Check native Codex skill directories, SKILL.md routing metadata, OpenAI UI metadata, and referenced resources. Use before publishing, installing, or changing any Codex skill.
---

# Codex Skill Checker

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Validate a skill structurally, then review whether it will route and execute well in real Codex work.

## Workflow

1. Locate the complete skill directory and read ../../references/codex-artifact-contracts.md.
2. Run the deterministic checker:

       python3 ../../scripts/check_codex_artifact.py skill .agents/skills/example-skill

3. Confirm the directory and frontmatter names match, the description says what and when, and no unsupported frontmatter keys exist.
4. Check agents/openai.yaml for concise UI metadata and a default prompt that explicitly mentions the skill.
5. Follow every local reference to scripts/, references/, assets/, or agents/; fail broken, escaping, or unsafe links.
6. Review progressive disclosure: keep routing and core workflow in SKILL.md and detailed facts in referenced files.
7. Test one explicit $skill-name request and one realistic implicit-routing request when a Codex runtime is available.

## Result

Return PASS only when deterministic and semantic checks succeed. Otherwise return FAIL with path, finding, impact, and the smallest corrective action. Never rewrite the skill unless asked.
