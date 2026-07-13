# NDDev Codex App

`nddev-codex-app` is a small, dependency-free setup manager for portable Codex
homes. It installs one of two explicit configuration sets without replacing or
deleting any unrelated `CODEX_HOME` state.

Version `0.1.0` is intentionally narrow. Inside the selected target it manages
only:

- `config.toml`,
- `AGENTS.md`,
- `NDDEV-CODEX-SETUP.json` (the module-owned state stamp).

It does not install Codex, plugins, hooks, MCP servers, skills, or custom
agents. It never infers or defaults to `~/.codex`.

## Requirements

- Python 3.10 or newer
- Codex CLI 0.138.0 or newer; this build is tested with Codex CLI 0.144.1
- a platform exposing Python's directory-FD and no-follow filesystem operations;
  mutating lanes are currently validated on macOS and Ubuntu
- an absolute, explicit target path for every command that reads or changes a
  Codex home

The machine-readable runtime baseline and its official source are recorded in
[`build/version.json`](build/version.json) and
[`references/codex-baseline.json`](references/codex-baseline.json).

## Setups

| Setup | Default Codex permission profile | Default approval policy |
| --- | --- | --- |
| `safe` | `:read-only` | `on-request` |
| `full-auto` | `:danger-full-access` | `never` |

Both setup configurations select built-in permission profiles through
top-level Codex scalars. The values are user configuration defaults, not
administrator-enforced policy: normal Codex configuration precedence, command
line overrides, and managed requirements still apply. `[permissions]` is the
current Codex surface for custom permission profiles; this module does not need
that table because both setups select built-ins. The generated configuration
does not mix permission profiles with legacy `sandbox_mode` settings or named
config profile tables.

## Usage

Run from a clone of this repository:

```bash
python3 cli-tools/nddev_codex.py list
python3 cli-tools/nddev_codex.py status --target /absolute/path/to/codex-home
python3 cli-tools/nddev_codex.py plan --setup safe --target /absolute/path/to/codex-home
python3 cli-tools/nddev_codex.py apply --setup safe --target /absolute/path/to/codex-home
python3 cli-tools/nddev_codex.py switch --setup full-auto --target /absolute/path/to/codex-home
python3 cli-tools/nddev_codex.py restore --backup 0 --target /absolute/path/to/codex-home
python3 cli-tools/nddev_codex.py remove --target /absolute/path/to/codex-home
python3 cli-tools/nddev_codex.py launch \
  --target /absolute/path/to/codex-home -- --version
```

`--target` selects the directory the manager reads or changes. It does not set
the parent shell's environment. `launch` first requires a clean managed target,
then starts `codex` with `CODEX_HOME` set to that target only in the child
environment. Arguments after `--` are forwarded unchanged, child standard I/O
is left attached, and the manager returns the child's exit code. Signal
termination uses the conventional shell status `128 + signal`.

After verifying the target separately, the direct environment form is also
available. It selects the same Codex home but does not run the manager's clean
state preflight:

```bash
CODEX_HOME=/absolute/path/to/codex-home codex
```

The lifecycle commands `list`, `status`, `plan`, `apply`, `switch`, `restore`,
and `remove` support `--json` for their manager results and errors. `launch`
accepts `--json` for manager preflight or spawn errors only; after a successful
spawn, child output remains unwrapped and attached directly to standard I/O.
`list` reads only the repository catalog and therefore takes no target. All
other commands require `--target`; relative targets and target symlinks are
rejected.

`apply` installs a missing target or updates the currently selected setup.
Changing setup identity requires `switch`. The manager refuses to overwrite an
unmanaged `config.toml` or `AGENTS.md`, and it refuses to mutate a managed file
whose digest or owner-only mode has drifted. `status` reports a target-level
`AGENTS.override.md`; `plan`, `apply`, `switch`, `restore`, and `launch` reject
it because Codex would load that file instead of the managed `AGENTS.md`,
making the selected setup instructions ineffective.

## Transaction and backup model

Mutations use a sibling exclusive lock, same-parent staging, verified managed
state, and rollback on failure. Before changing existing managed state, the
manager writes a target-bound envelope under:

```text
.<target-name>.nddev-codex-backups/<slot>/
  NDDEV-CODEX-BACKUP.json
  payload/
```

Slots are integers `0` through `9`. A free slot is used first; once all ten are
occupied, the oldest slot is rotated. Restore requires the exact slot and the
original canonical target. Non-managed files and directories inside the target
are never included in a backup and are never modified.

A target directory created by the manager uses mode `0700`. When the target
already exists, its directory mode is preserved. Managed files and their
backup payloads use mode `0600` and mode drift fails closed.

Locks and recovery holds are fail-closed. If a process is killed without a
chance to clean up, inspect the sibling lock or hold before removing it.

## Public/private boundary

This public repository contains runtime implementation, setup catalogs, public
contracts, documentation, and public repository automation. Module-specific
tests, fixtures, benchmarks, evidence, and release validation live in the
private `nddev-harnesses` control plane.

## License

Copyright © 2026 Danil Silantyev / NDDev. Licensed under
AGPL-3.0-or-later; see [LICENSE](LICENSE).
