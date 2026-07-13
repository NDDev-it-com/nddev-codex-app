---
name: codex-artifact-reviewer
description: Review a repository or directory containing Codex skills, plugins, marketplaces, agents, hooks, MCP, apps, config, instructions, or rules. Use for release readiness and cross-artifact quality audits.
---

# Codex Artifact Reviewer

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Run the complete deterministic suite, then review cross-artifact semantics and available runtime behavior without changing files.

## Workflow

1. Establish the review root, intended scope, trust boundary, and changed files.
2. Read ../../references/codex-artifact-contracts.md and inventory every supported Codex artifact.
3. Run the aggregate checker:

       python3 ../../scripts/check_codex_artifact.py all .

4. Run each repository-native validator relevant to changed artifacts. Never treat a missing runtime check as a pass.
5. Review cross-artifact ownership:
   - skills own workflows; no copied slash commands;
   - plugins bundle supported capabilities, while custom agents remain standalone TOML;
   - marketplaces point to canonical plugins without duplicating their truth;
   - hooks, MCP, apps, config, instructions, and rules have distinct policy and secret boundaries.
6. Use temporary HOME and CODEX_HOME for runtime discovery, parsing, installation, hook, MCP, or rules checks. Do not touch live Codex state.
7. Inspect portability, error paths, secret exposure, deprecated surfaces, unsafe authority, and stale documentation.

## Result

Return exactly one overall verdict: PASS or FAIL.

For FAIL, list findings by severity. Every finding must include the file, precise location, evidence or reproduction, impact, and corrective action. For PASS, list deterministic and runtime commands executed plus any checks that were unavailable; do not hide coverage gaps.
