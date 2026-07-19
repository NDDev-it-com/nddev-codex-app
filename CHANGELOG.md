# Changelog

All notable changes to this project are documented here.

## [0.3.10] - 2026-07-19

### Fixed

- The `requirements` checker now matches the exact Codex 0.144.6
  `ConfigRequirementsToml` surface, correcting fail-closed defects that rejected
  valid managed files. The managed key set uses `experimental_network` (not
  `network`) and accepts the `feature_requirements` alias for `features`;
  `allowed_permission_profiles` entries that are not built-ins are no longer
  rejected (they may be defined in a lower config layer); and `mcp_servers`,
  `features`, and other managed values are no longer validated with config.toml
  shapes. Permission-profile validation mirrors Codex exactly: `default_permissions`
  requires `allowed_permission_profiles`, the effective default must map to `true`,
  and the implicit `:workspace` default requires both `:workspace` and `:read-only`
  to be allowed.

### Added

- The `marketplace` checker discovers `.claude-plugin/marketplace.json` (the third
  manifest filename Codex recognizes) alongside `marketplace.json` and
  `api_marketplace.json`.
- `create_codex_artifact.py marketplace` gains `--source-type`
  (`local`/`url`/`git-subdir`/`npm`) with matching source fields, so the creator
  scaffolds every plugin source the checker accepts.

### Changed

- The builder plugin advances to 0.3.5 so an in-place `install-builder`
  re-materializes the cache after the checker and generator change.

## [0.3.9] - 2026-07-19

### Added

- nddev-builder gains an eleventh artifact family, managed `requirements.toml`,
  with `codex-requirements-creator` and `codex-requirements-checker` skills plus
  matching generator/checker support. The checker validates the managed layer's
  supported top-level keys, the `allowed_permission_profiles` table, and that a
  lower config layer always retains a valid default permission profile.
- The `marketplace` checker now accepts `local`, `url`, `git-subdir`, and `npm`
  plugin sources (previously local-only) and discovers a sibling
  `api_marketplace.json` catalog, matching the Codex 0.144.6 marketplace schema.
- The builder plugin advances to 0.3.4 (28 skills) so an in-place
  `install-builder` re-materializes the cache.

## [0.3.8] - 2026-07-19

### Added

- nddev-builder gains five workflow/lifecycle skills that turn the per-artifact
  creator/checker toolkit into a complete plugin/marketplace build cycle:
  `codex-builder-orientation` (index and routing), `codex-plugin-scaffolder`
  (compose a whole plugin from intent), `codex-plugin-devtest` (local dev-mode
  install/reinstall test loop), `codex-plugin-publish` (version, catalog, and
  publish), and `codex-release-review` (whole-bundle release-readiness). The
  builder plugin advances to 0.3.3 so an in-place `install-builder`
  re-materializes the cache.

## [0.3.7] - 2026-07-19

### Changed

- `install-builder` now enables the nddev-builder marketplace and plugin in the
  managed `config.toml` base -- a co-owned addition after the setup base -- so a
  plain `codex` launch loads the builder by default instead of only through
  `--profile nddev-builder`. The isolated `nddev-builder.config.toml` profile is
  still written for explicit `--profile` selection, and drift detection tolerates
  the enable as a runtime-style addition while keeping the setup base intact.
- When a setup `apply` or `switch` rewrites `config.toml` to the pure setup base,
  the co-owned builder enable is dropped while the cache and profile persist.
  `install-builder` then restores the base-config enable idempotently without
  re-materializing the cache or invoking the official Codex plugin commands.
- `builder-status` reports `config_enabled`; a target whose cache and profile are
  current but whose base-config enable is absent now reports `incomplete` rather
  than `installed`.

## [0.3.6] - 2026-07-18

### Changed

- Tested Codex CLI advanced to the official `rust-v0.144.6` release (published
  2026-07-18): version/tag/date, the SHA256SUMS manifest hash, and all four
  package sha256/size. `install.sh`/`install.ps1` remain byte-identical to the
  prior release, so those installer pins are unchanged.
- Bumped the nddev-builder plugin to 0.3.2 so an in-place `install-builder`
  re-materializes the plugin cache cleanly after the builder's config-schema
  provenance link advanced to 0.144.6 (the plugin cache is keyed by version).

## [0.3.5] - 2026-07-18

### Fixed

- The co-owned `config.toml` drift check no longer imports `tomllib`, so the
  manager keeps running on Python interpreters older than 3.11. The base-intact
  check is now line-based (every managed base `key = value` line must survive
  verbatim; the Codex runtime's `[projects.*]` additions are tolerated), which
  is sufficient for the controlled, simple setup base and carries no new runtime
  dependency.

## [0.3.4] - 2026-07-18

### Changed

- Tested Codex CLI advanced to the official `rust-v0.144.5` release
  (published 2026-07-16). All version pins, installer references, package
  checksums/sizes, and the builder's config-schema source link now point at
  0.144.5. The `install.sh`/`install.ps1` installers are byte-identical to
  0.144.4, so those pins are unchanged.

### Fixed

- `config.toml` is now treated as co-owned managed state. The Codex runtime
  persists project-trust decisions into it at launch (new
  `[projects."<workspace>"]` tables), which previously read as drift and could
  fail-close the next `launch`. Drift detection and the launch pre-check now
  verify the managed base keys are intact while tolerating the runtime's
  additions; a change to a managed base key, or any change to `AGENTS.md`, is
  still drift.

## [0.3.3] - 2026-07-14

### Changed

- Tested Codex CLI advanced to the official `rust-v0.144.4` release
  (2026-07-14; vendor-declared "No user-facing changes in this patch
  release"). All version pins, installer references, and the builder's
  config-schema source link now point at 0.144.4.
- `build/release-evidence.json` rebound to the 0.3.3 module content
  (execution-bound schema 2, pending until CI lane records exist).

## 0.3.2 - 2026-07-13

### Fixed

- Resolved CodeQL cleanup findings in the setup manager and builder generator
  while preserving fail-closed concurrent-replacement and rollback behavior;
  newly opened generator directory descriptors are now closed unless their
  ownership is transferred to the transaction registry.
- Updated release verification for manifest and contract schema 3 and included
  the `.agents` marketplace plus `plugins` tree in published archive and
  runtime bundles.

### Changed

- Advanced the immutable `nddev-builder` plugin cache identity to version
  `0.3.1` after the generator implementation changed.

## 0.3.1 - 2026-07-13

### Fixed

- Accept only the exact target-bound PATH-alias warning emitted by official
  Codex plugin commands when `CODEX_HOME` is below the platform temporary root;
  extra lines, altered roots, altered targets, and other diagnostics still fail
  closed.

## 0.3.0 - 2026-07-13

### Added

- Native `nddev-builder` marketplace and plugin with focused creator/checker
  skills for every supported Codex artifact family.
- Dependency-free artifact generator and static checker with transactional
  owner-only creation, bounded no-follow reads, and conservative overwrite,
  path, secret, permission-model, and schema guards.
- Official Codex marketplace installation contract that keeps plugin-owned
  state independent from the transactional `safe`/`full-auto` setup lifecycle.
- `install-builder` and `builder-status` lifecycle commands with a deterministic
  profile, exact bounded cache-tree validation, idempotence, and transactional
  configuration/profile/cache rollback.

### Changed

- Extended the public machine contract and manifest with the builder
  marketplace boundary, inventory, paths, version reference, and official
  documentation provenance.

## 0.2.1 - 2026-07-13

### Fixed

- Accept the bounded official temporary-directory PATH-alias diagnostic from
  the Codex version probe while continuing to reject unknown diagnostics and
  require one exact canonical `codex-cli <version>` stdout line.

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
