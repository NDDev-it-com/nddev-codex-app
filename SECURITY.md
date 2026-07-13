# Security Policy

## Supported surface

Security reporting covers the setup catalog, the lifecycle CLI, public
contracts, documentation, and GitHub workflows in this repository. Only the
latest numeric release is supported.

## Reporting a vulnerability

Report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/NDDev-it-com/nddev-codex-app/security/advisories/new).
Do not publish exploit details, credentials, tokens, private configuration, or
backup contents in an issue or pull request.

Include the affected command or path, reproduction steps, impact, and a
non-sensitive description of the environment. The maintainer aims to
acknowledge a report within 5 business days, triage it within 10 business days,
and provide a fix or mitigation plan for an accepted report within 30 business
days. These targets are best-effort.

## Baseline controls

- The CLI never defaults to `~/.codex`; target operations require an explicit
  absolute `--target`.
- The target, its managed files, backup pool, and catalog reject unsafe
  symlinks and special files. Managed files also reject hard-link aliases.
- Only `config.toml`, `AGENTS.md`, and `NDDEV-CODEX-SETUP.json` are changed.
- Existing unmanaged managed-path names and drifted managed files fail closed.
- Backup envelopes and installed stamps are bound to the canonical target.
- Mutations use an exclusive sibling lock, same-parent staging, bounded backup
  rotation, postcondition checks, and rollback on failure.
- Managed and backup files use owner-only permissions.
- Public workflows use least privilege and immutable action/workflow pins.
- Full behavioral, mutation, platform, and release validation remains in the
  private NDDev harness; no private fixtures or evidence are distributed here.

## Out of scope

- Codex runtime vulnerabilities not caused by this module.
- Modified forks or manual edits that bypass the lifecycle contract.
- Recovery after an uncatchable interruption where an operator deletes the
  fail-closed lock, recovery hold, or backup pool without inspection.
