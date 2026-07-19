---
name: codex-plugin-devtest
description: Install and iterate on a Codex plugin locally in developer mode: the install, reinstall, and cachebuster test loop for a plugin under development, run against an isolated install and never live state. Use when running or iterating on a plugin, its skills, MCP, hooks, or bundled app locally.
---

# Codex Plugin Devtest

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Run the local developer-mode loop — validate, register, install, edit, restart, verify — against an isolated install, never live ~/.codex.

## Workflow

1. Validate before installing. A bundle that fails either check does not get registered:

       python3 ../../scripts/check_codex_artifact.py plugin <plugin-dir>

   Then run the whole-bundle release review with $codex-artifact-reviewer and clear its findings.
2. Read ../../references/codex-artifact-contracts.md for the marketplace `local` source, plugin manifest, and .app.json schemas before editing any of them.
3. Create an isolated runtime so nothing touches live state, and export it for every command in the loop:

       work="$(mktemp -d)"
       export HOME="$work" CODEX_HOME="$work/.codex"

4. Stage the canonical layout inside that tree: place the plugin under ~/.codex/plugins/ (that is, $CODEX_HOME/plugins/).
5. Register it: add a `local` source entry that points at the plugin to $HOME/.agents/plugins/marketplace.json, or to a repo-root <repo>/.agents/plugins/marketplace.json.
6. Load the catalog from the isolated runtime, using the documented CLI surface rather than under-development plugin methods:

       codex plugin marketplace add <marketplace-root>
       codex plugin marketplace list

7. Launch Codex against the isolated CODEX_HOME, then browse, install, enable, and toggle the plugin through the `/plugins` command.
8. If the plugin bundles an app: enable Developer mode in ChatGPT, create the dev-mode app, copy its `plugin_asdk_app...` / `asdk_app_...` id, and wire that id into .app.json.
9. Iterate. After every edit, RESTART Codex: plugins are served from cache (remote plugins land under CODEX_HOME at plugins/cache/{marketplace}/{plugin}/{version}/), so the restart is the cachebuster — no edit is live until Codex reloads.
10. Verify discovery in the fresh session: confirm the plugin plus its skills, MCP, hooks, and app mapping are all discovered, then exercise exactly one read-only capability. Never install into or mutate the live ~/.codex.

## Quality bar

- Every register, install, discovery, and capability step runs under a temporary HOME and CODEX_HOME; the live ~/.codex and ~/.agents are never touched.
- Static plugin check and whole-bundle review are green before the plugin is registered.
- Re-run the static plugin check after each edit and before the restart, so a broken manifest never reaches the cache.
- Registration uses the documented marketplace and `/plugins` surface, not the under-development app-server plugin methods.
- After each edit a restart precedes re-verification, because Codex serves the cache, not the working tree.
- Discovery is observed, not assumed: plugin, skills, MCP, hooks, and app mapping appear in a fresh isolated session and one read-only capability succeeds.
- Developer-mode app ids come from ChatGPT and live only in .app.json; no placeholder ids or credential values are committed.
