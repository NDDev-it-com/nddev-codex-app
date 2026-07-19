---
name: codex-release-review
description: Audit a complete plugin/marketplace bundle for release readiness, verifying cross-artifact identity, versioning, policy, presentation, and coherence across every bundled Codex artifact. Use as the pre-publish gate after per-artifact review, to prove the whole product is shippable rather than that individual files merely parse.
---

# Codex Release Review

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Audit a complete plugin and marketplace bundle as one shippable product, proving every artifact is present, mutually consistent, and coherent before publish, without changing files. This is the whole-bundle step up from single-scope artifact review: it verifies the seams between artifacts, not only each file alone.

## Workflow

1. Establish the bundle root, the release scope, the trust boundary, the changed files, and the intended version bump.
2. Read ../../references/codex-artifact-contracts.md and inventory the plugin manifest, the marketplace catalog, and every bundled skill, hook, MCP server, app, and instruction.
3. Run the deterministic aggregate over the whole tree first, before any human judgment:

       python3 ../../scripts/check_codex_artifact.py all <root>

4. Run each per-artifact `-checker` sibling skill for the changed files, so every touched artifact clears its own contract:
   - codex-plugin-checker for the manifest and its bundled capability tree;
   - codex-marketplace-checker for the catalog and its reachable local plugins;
   - codex-skill-checker, codex-hook-checker, codex-mcp-checker, codex-app-checker, and the rest for their kinds.

   Never treat a missing runtime check as a pass.
5. Verify cross-artifact consistency; the contracts reference owns each field's schema:
   - identity: the plugin manifest `name` equals the plugin folder equals the marketplace `plugins[].name`;
   - version: SemVer is present and is bumped whenever any bundled content changed since the last release;
   - catalog: each marketplace entry carries `policy.installation`, `policy.authentication`, and `category`, `plugins[]` order is preserved, and any parallel `api_marketplace.json` stays in sync;
   - presentation: `interface.defaultPrompt` holds at most three entries of at most 128 characters each, and every screenshot resolves to a real PNG asset;
   - skills: review skills follow the `code-review-*` auto-routing naming where applicable, and no skill bundles README, INSTALLATION, or CHANGELOG; ship only what the agent needs at runtime;
   - components: manifest component keys are only skills, hooks, mcpServers, and apps; `mcpServers` and `hooks` accept string, array, or inline-object shapes; custom subagent roles stay standalone TOML; and no authored slash commands appear;
   - instructions: any bundled AGENTS.md guidance names only commands and paths that still exist in the shipped bundle.
6. Exercise discovery and parsing in a temporary HOME and CODEX_HOME: register the bundle through a throwaway marketplace and confirm the plugin and its skills are found. Never touch the live ~/.codex.
7. Report runtime-only facts separately from static shape: authentication, trust prompts, transport, and tool behavior, each pinned to the exact Codex version and isolated paths.
8. Inspect portability, secret exposure, deprecated surfaces, unsafe authority, and stale documentation across the entire bundle.
9. When every check is clean, hand off to codex-plugin-publish.

## Result

Return exactly one overall verdict: PASS or FAIL.

For FAIL, list findings by severity. Every finding must include the file, the precise location, evidence or a reproduction, the impact, and the corrective action. For PASS, list the deterministic and runtime commands executed plus any checks that were unavailable; never hide a coverage gap. PASS means the whole bundle is coherent and shippable, not merely that each file parses.

## Quality bar

- The aggregate checker and every relevant per-artifact checker ran, and their coverage gaps are named, not hidden.
- Every bundled skill, hook, MCP server, and app passed its own checker, and the plugin passed as a distributable boundary.
- One product identity: manifest, folder, and catalog agree, and the version reflects the actual content delta.
- The marketplace entry is installable exactly as written, with explicit installation and authentication policy.
- Every path, launcher, and asset is portable and resolves inside its own artifact boundary.
- No credential, live session, trust hash, or machine-specific state ships in any bundled file.
- The bundle carries only what an agent needs at runtime: no README, INSTALLATION, or CHANGELOG, and no copied slash-command tree.
- Static shape and runtime discovery are reported separately, each with the exact Codex version and isolated paths.
