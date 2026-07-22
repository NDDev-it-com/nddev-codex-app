# Changelog

All notable changes to `nddev-codex-app` are documented here.

## [0.1.0] - Unreleased

Pre-release baseline. Version scheme realigned across the nddev setup modules:
`0.1.0` reflects that the `nddev-builder` tooling — the setup system for
building setups — is ready, while the working setups themselves are not yet
shipped. `1.0.0` is reserved for the first working setups.

- Codex setup manager (`cli-tools/nddev_codex.py`) with target-explicit
  lifecycle and the co-owned `config.toml` ownership model.
- Native `nddev-builder` marketplace and plugin, including the config,
  requirements, and artifact checkers.
- Checkers and runtime pinned to the official Codex CLI `0.145.0`
  (`rust-v0.145.0`); the standalone `install.sh` installer and the
  per-platform package archives are verified against the official
  `codex-package_SHA256SUMS` manifest.
