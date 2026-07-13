# Changelog

All notable changes to this project are documented here.

## 0.1.0 - 2026-07-13

### Added

- Target-explicit setup lifecycle for `safe` and `full-auto`.
- Deterministic Codex config and instruction rendering.
- Target-bound managed stamps and ten-slot backup envelopes.
- Managed-target Codex launch with child-only `CODEX_HOME`, direct argument and
  standard-I/O forwarding, and preserved child exit status.
- Locked mutations, rollback, JSON output, and public release/security
  automation skeletons.

### Changed

- Declared Codex CLI 0.138.0 as the compatibility floor and 0.144.1 as the
  tested runtime baseline for the beta permission-profile surface.
- Preserved an existing target directory's mode while enforcing owner-only
  modes for manager-created targets, managed files, and backup payloads.
- Rejected `AGENTS.override.md` so the managed `AGENTS.md` remains effective.
- Clarified that setup permissions are user defaults subject to Codex
  configuration precedence and managed requirements.
