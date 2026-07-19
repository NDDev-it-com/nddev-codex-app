---
name: codex-onboarding
description: A guided first run of the nddev-builder toolkit — from zero to a working, checked Codex artifact in ordered steps. Use when new to nddev-builder, onboarding, or unsure which builder skill to start with.
---

# Codex Onboarding

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

The fastest correct path from nothing to a working, validated Codex artifact.
Read `$codex-builder-orientation` once for the family map, pick a path below,
and let each step hand off to the focused creator, checker, or workflow skill.

## The one thing to understand first

Choose the **smallest native surface** the task needs, because Codex validates
and loads each one differently:

- `AGENTS.md` for durable repository guidance; a **skill** for a reusable
  workflow; a **plugin** to distribute skills/hooks/MCP/apps; **MCP or an app**
  for a live external capability; a **hook** for lifecycle enforcement; a
  **rule** for outside-sandbox command policy; and a **config/permission
  profile** for model and safety posture.
- Codex has **no first-class authored slash commands** — a reusable command
  workflow is authored as a skill.
- Custom-agent TOML is a **standalone config-scope file**
  (`$CODEX_HOME/agents/<name>.toml` or `<repo>/.codex/agents/<name>.toml`), not
  a plugin-bundled component.

The family list, schemas, and current (Beta/experimental/under-development)
surfaces live in ../../references/codex-artifact-contracts.md and the two script
`--help` outputs — read them, never a frozen copy.

## Path 1 — add one artifact to an existing repo or config

1. Name the family (`skill`, `config`, `hook`, `mcp`, `agent`, `rule`,
   `instructions`, `app`, `requirements`).
2. Author with the owning creator: `$codex-<family>-creator` (for example
   `$codex-skill-creator`). It stages a conservative skeleton.
3. Complete the content, then validate with `$codex-<family>-checker` (or
   `python3 ../../scripts/check_codex_artifact.py <family> <path>`).
4. Prove behavior with the runtime step in that family's contract, using a
   temporary `HOME`/`CODEX_HOME` — a static PASS proves shape only.

## Path 2 — build a whole plugin from an idea

1. `$codex-plugin-scaffolder` — compose the bundle (manifest plus the skills,
   hooks, MCP servers, and app mapping the idea needs) from intent.
2. `$codex-plugin-devtest` — register through a temporary marketplace with
   isolated `CODEX_HOME` and confirm discovery and install.
3. `$codex-release-review` — gate the whole bundle for release readiness.
4. `$codex-plugin-publish` — version, catalog, and publish when others install
   it.

## Path 3 — start or distribute a marketplace

1. `$codex-marketplace-creator` — scaffold the catalog (`local`, `url`,
   `git-subdir`, or `npm` plugin sources via `--source-type`).
2. Add plugins with Path 2, then `$codex-release-review`.
3. `$codex-plugin-publish` for distribution; register and inspect only with the
   exact target-owned Codex CLI.

## Golden rules (from day one)

- Read `$codex-builder-orientation` first; author with exactly one owning skill
  and defer to its description.
- A static generate or check proves shape only — always run the matching
  checker and then the contract's runtime step in isolated state.
- Full `agent` and `config` TOML validation needs Python 3.11+; on 3.10 the
  checker fails closed rather than returning an unchecked PASS.
- Keep credentials, OAuth tokens, trust hashes, and machine-local paths out of
  every artifact; reference secrets through the environment.
- English only. Never mix permission profiles with legacy `sandbox_mode` in one
  loaded layer.

## Where to go deeper

`$codex-builder-orientation` for the full family map and ownership boundaries,
and ../../references/codex-artifact-contracts.md for every artifact's exact
contract, runtime check, and current maturity.
