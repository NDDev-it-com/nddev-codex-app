# Changelog

All notable changes to this project are documented here.

## 0.2.0 - 2026-07-13

### Added

- Target-owned installation and update of the exact official Codex CLI
  `0.144.3` standalone release through its verified `install.sh` asset.
- Non-mutating `software-status` with validated package layout, metadata,
  executable, and version reporting.
- macOS `desktop` delegation to the stable official `codex app` command, with
  an optional validated absolute workspace.

### Changed

- `launch` now requires the validated target-owned Codex CLI instead of
  resolving an ambient `codex` from `PATH`.
- Installer execution now isolates home, temporary state, and `PATH`; JSON
  output remains free of installer prose, and abnormal exits terminate the
  complete installer process group.
- Public product language distinguishes this setup manager from the current
  ChatGPT desktop app and its Chat, ChatGPT Work, and Codex modes.
- Desktop delegation records the pinned CLI's legacy `Codex.app` packaging and
  does not claim that the GUI inherits the selected target configuration.

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
