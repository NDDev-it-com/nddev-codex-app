<!--
GENERATED FILE - DO NOT EDIT DIRECTLY
generator: gds
bundle: 0.1.0-dev
source-commit: f77ee6ece659be46309117db01b81f3255f9a552
input-digest: sha256:1500a454bbd8d1c12b192f7d31c9b3678c19a2db4e1dad78863038810d058307
output-digest: sha256:aeb6e32ba702c91ab6c757cf2404b54a5d9599550796a44918d30d36ad75c020
edit-source:
  - .gds/repository.yaml
  - policies/base/repository-default.yaml
  - policies/owners/organization-default.yaml
  - policies/roles/public-module.yaml
  - templates/agents/repository.md.tmpl
  - templates/github-actions/go.yml.tmpl
  - templates/harnesses/claude.md.tmpl
-->
# GDS repository contract

## Scope

- Repository ID: `repo_01KXEDY8XHS7RFV8KTJHDTZ8NF`.
- Roles: `module`.
- Canonical repository facts: `.gds/repository.yaml`.
- Applied bundle: `.gds/bundle.lock.yaml` (`0.1.0-dev`).
- Compiled policy: `.gds/compiled-policy.json`.

## Boundaries

- This Git repository is one independent mutation boundary.
- Preserve unrelated branches, worktrees, submodules, and dirty changes.
- Resolve cross-repository work with `gds context --json` before acting.
- Generated files are projections; change their canonical inputs and regenerate.

## Safety

- External writes require explicit approval: `true`.
- Generated projection edits: `forbidden`.
- Private parent context persistence: `forbidden`.
- Visibility contract: `public`; data classification: `public`.

## Development

- Test: `python3 cli-tools/validate_public_contracts.py`.

## Agent routing

- Active skill profiles: `core, module`.
- Use on-demand skills for procedures; do not duplicate them here.
- Treat docs and memories as derived evidence, not mutation authority.

## Done

- Required verification is complete or explicitly `NOT_PROVEN`.
- Git state and every affected repository boundary are classified.
- No private data, secret, or unapproved generated drift is introduced.
