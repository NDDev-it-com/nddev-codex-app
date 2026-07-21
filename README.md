# NDDev Codex Setup Manager

`nddev-codex-app` is a dependency-free manager for a caller-selected Codex
home. Version `0.3.12` installs the exact tested official Codex CLI standalone
release into that target and switches one of two complete NDDev configuration
sets without deleting unrelated target state. The repository also publishes
the independently installable `nddev-builder` Codex marketplace.

The current OpenAI desktop product is the ChatGPT app for macOS and Windows. It
contains separate Chat plus selectable ChatGPT Work and Codex modes. Codex CLI
`0.144.6` still implements `codex app` through legacy `Codex.app`/`Codex.dmg`
packaging. This module does not manage either desktop bundle directly; on
macOS, `desktop` is only a narrow delegation to that upstream command.

## Owned state

The setup lifecycle manages only:

- `config.toml`,
- `AGENTS.md`,
- `NDDEV-CODEX-SETUP.json`.

The software lifecycle owns the official standalone layout under the same
explicit target:

```text
bin/codex
packages/standalone/current
packages/standalone/releases/<version>-<platform>/
```

Credentials, sessions, and entries unrelated to the selected lifecycle remain
untouched. Setup switching and removal do not modify the standalone software
layout, builder profile, or Codex-owned builder cache. The manager never infers
or defaults to `~/.codex`.

## Requirements

- Python 3.10 or newer
- macOS or Linux on ARM64 or x86-64 for `install-cli` and `update-cli`
- macOS for the `desktop` delegation command
- Python directory-FD and no-follow filesystem operations for mutations
- an absolute target whose parent already exists

This build installs and requires Codex CLI `0.144.6`. The configuration format
remains compatible with Codex CLI `0.138.0` or newer.

## Install the official Codex CLI

```bash
python3 cli-tools/nddev_codex.py software-status \
  --target /absolute/path/to/codex-home
python3 cli-tools/nddev_codex.py install-cli \
  --target /absolute/path/to/codex-home
python3 cli-tools/nddev_codex.py update-cli \
  --target /absolute/path/to/codex-home
```

Install and update first download the pinned OpenAI `rust-v0.144.6`
`install.sh` asset into a temporary directory, enforce its exact size and
SHA-256, and invoke it without a shell pipeline. That verified official
installer then downloads the pinned checksum manifest and host package and
validates their release digests and package checksum. `CODEX_HOME`,
`CODEX_INSTALL_DIR`, `CODEX_NON_INTERACTIVE`, and `CODEX_RELEASE` are fixed by
the manager. Isolated `HOME`, `USERPROFILE`, and `TMPDIR`, plus a controlled
system-tool `PATH`, exclude user shell startup state and user package-manager
paths; installer package state is written only below the explicit target.

After installation, the manager validates the target-owned `current` release,
the exact `codex-package.json` schema, host platform identity, visible command
symlinks, compatibility entrypoint, code-mode host, bundled ripgrep, Linux
sandbox helper, executable ownership/modes, bounded `codex --version` output,
the official temporary-directory PATH-alias diagnostic when present, and exact
canonical stdout version. `install-cli` and `update-cli` are idempotent when
`0.144.6` is already current. A different installed version must be advanced
with `update-cli`.

`software-status` is non-mutating and reports `installed`, `current`, `version`,
and `executable`. Partial or unsafe standalone layouts fail closed rather than
being reported as healthy.

## Setups

| Setup | Default permission profile | Default approval policy |
| --- | --- | --- |
| `safe` | `:read-only` | `on-request` |
| `full-auto` | `:danger-full-access` | `never` |

These are user defaults. Project configuration, command-line overrides, and
administrator-managed requirements retain their normal Codex precedence. The
generated configuration does not mix permission profiles with legacy
`sandbox_mode` or named config profile tables.

```bash
python3 cli-tools/nddev_codex.py list
python3 cli-tools/nddev_codex.py status \
  --target /absolute/path/to/codex-home
python3 cli-tools/nddev_codex.py plan --setup safe \
  --target /absolute/path/to/codex-home
python3 cli-tools/nddev_codex.py apply --setup safe \
  --target /absolute/path/to/codex-home
python3 cli-tools/nddev_codex.py switch --setup full-auto \
  --target /absolute/path/to/codex-home
python3 cli-tools/nddev_codex.py restore --backup 0 \
  --target /absolute/path/to/codex-home
python3 cli-tools/nddev_codex.py remove \
  --target /absolute/path/to/codex-home
```

`apply` installs a missing target or updates the current setup. `switch` is
required to change setup identity. Unmanaged `config.toml` or `AGENTS.md`,
managed drift, unsafe links, and a target-level `AGENTS.override.md` fail
closed.

## Install NDDev Builder

`nddev-builder` is a native Codex marketplace and plugin, not a third
permission setup. The manager keeps reusable authoring capabilities independent
from the selected `safe` or `full-auto` posture: Codex owns the plugin cache,
while NDDev enables the builder in the managed `config.toml` base (default-on
for a plain `codex` launch) and also writes one deterministic
`nddev-builder.config.toml` profile for explicit `--profile` selection.

After applying a setup and installing the target-owned CLI, install and inspect
the builder through the manager:

```bash
python3 cli-tools/nddev_codex.py install-builder \
  --target /absolute/path/to/codex-home --json
python3 cli-tools/nddev_codex.py builder-status \
  --target /absolute/path/to/codex-home --json
python3 cli-tools/nddev_codex.py launch \
  --target /absolute/path/to/codex-home
```

A plain `launch` loads the builder by default; append `-- --profile nddev-builder`
only when you want to activate it explicitly through the profile instead.

`install-builder` and `launch` require the target to already be the current
catalog setup. A target that was applied by an older manager build carries a
stale `NDDEV-CODEX-SETUP.json` stamp and fails closed with `managed target is
not the current canonical catalog setup; run apply --setup <id> before launch`.
Re-run `apply --setup <id>` first: it re-stamps the setup to the current catalog
and archives the prior managed state to a numbered backup slot. Approved co-owned
overlays -- the builder enable, any user `[mcp_servers.*]` entries, and the Codex
runtime's `[projects.*]` trust -- are carried onto the new base transactionally,
so nothing has to be re-added by hand. An addition outside those approved
namespaces (for example a `[sandbox_workspace_write]` that changes the managed
posture) reads as drift and must be resolved before apply, switch, or launch.

`install-builder` invokes the target-owned pinned Codex CLI's official
`plugin marketplace add` and `plugin add` commands with bounded output and a
timeout. When a target is under the platform temporary root, it accepts only
the exact target-bound PATH-alias warning emitted by Codex and continues to
reject every other diagnostic. It validates the complete bounded plugin cache
tree against the source, including the exact manifest and version, then enables
the builder in the managed `config.toml` base as a co-owned addition after the
setup base -- so a plain `codex` launch loads it by default -- while also
writing the stable marketplace/plugin activation into the owner-only
`nddev-builder.config.toml` profile for explicit `--profile` selection. Failure
rolls back the configuration, profile, and the bounded versioned cache tree to
its exact prior bytes and modes;
`AGENTS.md`, the setup stamp, unrelated plugins, and setup permissions remain
unchanged.
The profile and Codex-owned plugin cache are preserved across setup switches
and setup removal; a setup apply or switch also preserves the co-owned builder
enable, so the builder stays default-on across a switch without re-running
`install-builder`.

The marketplace exposes skills for creating and checking every supported
builder artifact family: skills, plugins, marketplaces, custom-agent TOML,
hooks, MCP configuration, ChatGPT app mappings, Codex config, `AGENTS.md`,
execpolicy rules, and managed `requirements.toml`, plus workflow skills that
orient and route, scaffold a whole plugin from intent, run the local dev-mode
test loop, publish, and review a bundle for release. The marketplace checker
accepts `local`, `url`, `git-subdir`, and `npm` plugin sources. It intentionally ships no copied slash-command files and no
plugin-bundled custom agents because neither is a native Codex plugin surface.
See `plugins/nddev-builder/README.md` for its exact inventory and checker use.

## Launch CLI or delegate the desktop bridge

```bash
python3 cli-tools/nddev_codex.py launch \
  --target /absolute/path/to/codex-home -- --version
python3 cli-tools/nddev_codex.py launch \
  --target /absolute/path/to/codex-home -- \
  --profile nddev-builder
python3 cli-tools/nddev_codex.py desktop \
  --target /absolute/path/to/codex-home
python3 cli-tools/nddev_codex.py desktop \
  --target /absolute/path/to/codex-home \
  --workspace /absolute/path/to/project
```

Both commands require the exact current target-owned CLI and never resolve
`codex` from ambient `PATH`. `launch` additionally requires a clean canonical
setup and forwards Codex arguments unchanged. `desktop` invokes exactly
`codex app` with no argument, or with one validated absolute workspace;
arbitrary upstream installer/source flags are not exposed. It does not require
a managed setup because Codex CLI `0.144.6` passes only a workspace URL to the
GUI: setting `CODEX_HOME` for the bridge process is not a guarantee that the
desktop application will inherit the selected target configuration. Both keep
standard I/O attached and return the child exit code.

After separate validation, direct execution is also possible without the
manager's clean-state preflight:

```bash
CODEX_HOME=/absolute/path/to/codex-home \
  /absolute/path/to/codex-home/bin/codex
```

Lifecycle, software, and builder result commands support `--json`. `launch` and
`desktop` use JSON only for manager preflight or spawn errors; successful child
output remains raw.

## Transactions and backups

Configuration mutations use an exclusive sibling lock, same-parent staging,
descriptor-anchored identity checks, owner-only managed files, and rollback on
failure. Before changing existing managed state, a target-bound backup is
published under `.<target-name>.nddev-codex-backups/<slot>/`. Slots `0` through
`9` rotate oldest-first. Unmanaged target entries are never backed up or
modified.

Software installation additionally uses the official standalone install lock
inside the target while the NDDev sibling target lock prevents concurrent
setup launch or mutation.

## Public/private boundary

This public repository contains runtime implementation, setup catalogs, public
contracts, documentation, and public repository automation. Tests, fixtures,
benchmarks, evidence, and release validation remain in private
`nddev-harnesses`.

## License

Copyright © 2026 Danil Silantyev / NDDev. Licensed under
AGPL-3.0-or-later; see [LICENSE](LICENSE).
