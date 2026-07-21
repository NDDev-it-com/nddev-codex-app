<!--
GENERATED FILE - DO NOT EDIT DIRECTLY
generator: gds
bundle: 0.1.0-dev
source-commit: f77ee6ece659be46309117db01b81f3255f9a552
input-digest: sha256:1500a454bbd8d1c12b192f7d31c9b3678c19a2db4e1dad78863038810d058307
output-digest: sha256:c183d8d3b5028ad0bebae191382037853962850aba6457d09d43c191b636c002
edit-source:
  - .gds/repository.yaml
  - policies/base/repository-default.yaml
  - policies/owners/organization-default.yaml
  - policies/roles/public-module.yaml
  - templates/agents/repository.md.tmpl
  - templates/github-actions/go.yml.tmpl
  - templates/harnesses/claude.md.tmpl
-->
# Claude Code repository contract

## Scope

- GDS repository ID: `repo_01KXEDY8XHS7RFV8KTJHDTZ8NF`.
- Roles: `module`.
- Canonical repository facts: `.gds/repository.yaml`.
- Applied policy bundle: `.gds/bundle.lock.yaml` (`0.1.0-dev`).
- This is a first-class Claude Code projection compiled from the same typed
  inputs as `AGENTS.md`; neither projection is a manual policy source.

## Repository boundaries

- Treat this Git repository as one independent mutation boundary.
- Preserve unrelated dirty changes, branches, worktrees, and submodules.
- Run `gds context --json` before work crosses repository boundaries.
- Do not edit generated projections; change the declared canonical input and
  regenerate.

## Safety

- External writes require explicit approval: `true`.
- Generated projection edits: `forbidden`.
- Private parent context persistence: `forbidden`.
- Visibility: `public`; data: `public`.

## Verification commands

- Test: `python3 cli-tools/validate_public_contracts.py`.

## Claude workflow routing

- Active skill profiles: `core, module`.
- Load procedural detail from the applicable installed GDS skill projection or
  plugin only when the task matches it.
- Destructive workflows remain explicit-only and still require their concrete
  plan and approval gates.
- Treat documentation and Serena memories as derived evidence, never mutation
  authority.

## Done

- Required checks pass or are explicitly reported `NOT_PROVEN`.
- Every affected Git boundary and remote result is classified.
- No secret, private-context leak, unrelated change, or unapproved projection
  drift is introduced.
