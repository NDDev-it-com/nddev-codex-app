---
name: codex-plugin-scaffolder
description: Build a whole Codex plugin from intent, not a single artifact. Define the product boundary, scaffold the manifest and every bundled capability (skills, hooks, MCP servers, apps) plus an optional personal marketplace entry, and validate the complete bundle. Use to compose a coherent multi-capability plugin rather than one standalone file.
---

# Codex Plugin Scaffolder

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Orchestrate a complete, coherent plugin from intent by composing the per-artifact creators into one product boundary, rather than hand-authoring each file in isolation. Treat the manifest, its bundled capabilities, and any marketplace entry as a single deliverable that must agree end to end.

## Workflow

1. Prefer the built-in $plugin-creator for first-party scaffolding and manifest guidance. Use this skill when the plugin must be repo-native, bundle several capabilities, and be seeded into a personal marketplace in one coherent pass.
2. Define the product boundary before creating anything: the plugin's purpose and audience, each capability it will bundle and the reason it belongs, the installation and authentication policy, and whether it needs a personal marketplace entry.
3. Read ../../references/codex-artifact-contracts.md for the manifest and component schemas, the component keys plugin.json recognizes, the rule that subagent roles stay standalone TOML outside the bundle, and the rule that a commands/ tree is never authored.
4. Scaffold the manifest shell and keep .codex-plugin/plugin.json at the plugin root so every capability path resolves from there:

       python3 ../../scripts/create_codex_artifact.py plugin --help
       python3 ../../scripts/create_codex_artifact.py plugin --output plugins --name example-plugin --description "Bundle a focused Codex workflow product"

5. Give the manifest a stable identity — kebab-case name and an initial SemVer version — and complete its presentation metadata per the contracts reference.
6. Plan the build order by dependency so each capability validates against real siblings — an MCP server before the app that surfaces it, or a hook before a skill that assumes it.
7. Compose each bundled capability under the plugin root, one invocation per family, and author its content with the matching codex-<family>-creator skill; the script owns every family and flag:

       python3 ../../scripts/create_codex_artifact.py skill --help
       python3 ../../scripts/create_codex_artifact.py skill --output plugins/example-plugin/skills --name example-workflow --description "One reusable capability inside the bundle"

8. For any bundled MCP server or app, keep credentials in environment or OAuth flows and declare required secrets in policy, never in bundled files.
9. Pin launcher package or binary versions for any bundled MCP server so bundle startup stays reproducible across installs.
10. Wire each capability into the manifest and declare only capabilities that exist on disk, ordering them so the plugin reads as one product rather than a pile of files.
11. Keep subagent roles as standalone TOML per the contracts reference, outside the bundle, and never translate Claude or OpenCode slash commands into a commands/ tree.
12. Optional: seed a personal marketplace entry with codex-marketplace-creator per the contracts reference — a local source pointing at this plugin, explicit installation and authentication policy, and a plugins[] order matching the intended render order.
13. Validate each bundled capability with its matching codex-<family>-checker as you compose it, so a failure stays localized.
14. Reconcile the manifest against the filesystem so no capability is declared-but-missing or present-but-undeclared.
15. Review the assembled bundle with codex-release-review.
16. Run codex-plugin-devtest for the local dev-mode discovery and install loop.
17. Refresh Codex discovery if a newly bundled capability does not appear; a static PASS proves shape only, not runtime behavior.
18. Run every discovery, install, or lifecycle check under a temporary HOME and CODEX_HOME; never touch the live ~/.codex.

## Quality bar

- The product boundary is explicit: each bundled capability serves the stated purpose, with no orphaned or duplicated responsibility.
- Manifest identity, version, and capability paths agree with the filesystem, and only capabilities present on disk are declared.
- Installation, authentication, and any marketplace policy are explicit and least-surprising; the plugins[] order equals the intended render order.
- Subagent roles remain standalone TOML and no commands/ tree is authored.
- Bundled files are portable and secret-free and use relative paths that stay inside the plugin root.
- Each capability is built and validated incrementally, so failures are localized before the whole bundle is reviewed.
- The whole bundle reads as one reviewable change, and the provenance of each capability — the creator that produced it — stays clear.
- Every capability passes its own checker, the bundle passes codex-release-review, and codex-plugin-devtest confirms discovery in isolated state without touching live user state.
