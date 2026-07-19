---
name: codex-marketplace-creator
description: Create or revise a Codex marketplace catalog with valid plugin sources, categories, installation policy, and authentication policy. Use when publishing local or remote plugin collections.
---

# Codex Marketplace Creator

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Create a small, auditable catalog that points to canonical plugin sources without duplicating plugin-owned data.

## Workflow

1. Inventory the plugins, source type, trust boundary, release policy, and authentication needs.
2. Read ../../references/codex-artifact-contracts.md for the current marketplace schema.
3. Inspect options and generate the catalog shell:

       python3 ../../scripts/create_codex_artifact.py marketplace --help
       python3 ../../scripts/create_codex_artifact.py marketplace --output . --name example-marketplace --description "Curated Codex plugins for this repository" --plugin-name example-plugin

4. Add stable plugin identifiers and resolvable sources. Keep each category and policy aligned with actual behavior.
5. Select the source with `--source-type`: `local` for repository development, or `url`, `git-subdir`, or `npm` for remote releases. Prefer immutable remote references (`ref`/`sha` or a pinned `npm` version) for releases.
6. Do not embed credentials, environment values, or duplicate plugin manifests.
7. Validate the catalog and all reachable local plugins:

       python3 ../../scripts/check_codex_artifact.py marketplace .agents/plugins/marketplace.json

8. When supported, add and list the marketplace in an isolated CODEX_HOME before delivery.

## Quality bar

- Plugin ids are unique and sources cannot escape the intended root.
- Installation and authentication policies are explicit.
- Every local source resolves to a valid native Codex plugin.
- Catalog changes are reviewable and version provenance is clear.
