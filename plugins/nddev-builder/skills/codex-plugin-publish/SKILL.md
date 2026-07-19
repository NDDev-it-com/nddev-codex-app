---
name: codex-plugin-publish
description: Version, catalog, and publish a native Codex plugin for distribution — the release step after dev-test, covering the SemVer manifest bump, marketplace catalog update, distribution source, and portal submission.
---

# Codex Plugin Publish

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Release a validated plugin as an installable, discoverable distribution without shipping an unproven bundle or duplicating the schema the catalog owns.

## Workflow

1. Gate before anything else: never publish an unvalidated bundle. Run the whole-bundle release review ($codex-release-review) and the aggregate checker, then resolve every finding:

       python3 ../../scripts/check_codex_artifact.py all <root>

2. Confirm the working tree holds exactly the content you intend to ship, because the bundle you validate is the bundle that gets cached under the new version.

3. Decide the SemVer impact (patch, minor, or major) from what changed across the plugin since its last release.

4. Bump `version` in .codex-plugin/plugin.json. The plugin version keys the install cache, so any content change needs a version bump for a clean reinstall.

5. Update this plugin's entry in <repo>/.agents/plugins/marketplace.json, and mirror the change into the parallel .agents/plugins/api_marketplace.json when you serve API-key-login users. Read ../../references/codex-artifact-contracts.md and $codex-marketplace-creator for the exact schema; do not restate it here.

6. Preserve plugins[] render order: append a new plugin, never reorder existing entries.

7. Set policy.installation (NOT_AVAILABLE, AVAILABLE, or INSTALLED_BY_DEFAULT), policy.authentication (ON_INSTALL or ON_USE), and category to match the plugin's real behavior and trust.

8. Pick one distribution source — local, url (Git), git-subdir, or npm. Remote plugins are enabled by default as of Codex 0.143 and cache a verified .tar.gz per version; prefer an immutable reference for a release and a local relative source for development.

9. Submit the plugin through the Codex Submit-plugins portal (developers.openai.com/codex, submit-plugins). Apps are submitted as plugins.

10. Verify the published source end to end: install from it in a temporary HOME and CODEX_HOME, confirm discovery and the resolved version, and never touch the live ~/.codex.

## Quality bar

- Nothing was catalogued or submitted before the whole-bundle review and aggregate check passed clean.

- The SemVer version increased for the change and matches both the catalog entry and the cached source.

- Installation, authentication, and category policies are explicit and match observed behavior.

- plugins[] order is unchanged and no existing entry was reordered.

- The catalog points at one canonical, resolvable source and duplicates no plugin-owned truth.

- No credentials, tokens, or live runtime state entered the manifest, catalog, or cached archive.

- Post-publish discovery was proven from isolated HOME and CODEX_HOME, never the owner's live Codex home.
