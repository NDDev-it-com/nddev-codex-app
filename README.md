# NDDev Codex App

`nddev-codex-app` is a small, dependency-free setup manager for portable Codex
homes. It installs one of two explicit configuration sets without replacing or
deleting any unrelated `CODEX_HOME` state.

Version `0.1.0` is intentionally narrow. It manages only:

- `config.toml`,
- `AGENTS.md`,
- `NDDEV-CODEX-SETUP.json` (the module-owned state stamp).

It does not install Codex, plugins, hooks, MCP servers, skills, or custom
agents. It never infers or defaults to `~/.codex`.

## Requirements

- Python 3.11 or newer
- an absolute, explicit target path for every command that reads or changes a
  Codex home

## Setups

| Setup | Codex permission profile | Approval policy |
| --- | --- | --- |
| `safe` | `:read-only` | `on-request` |
| `full-auto` | `:danger-full-access` | `never` |

Both setup configurations use top-level Codex scalars. They do not mix the
permission-profile model with `sandbox_mode`, a `[permissions]` table, or
legacy profile tables.

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
```

Every command supports `--json`. `list` reads only the repository catalog and
therefore takes no target. All other commands require `--target`; relative
targets and target symlinks are rejected.

`apply` installs a missing target or updates the currently selected setup.
Changing setup identity requires `switch`. The manager refuses to overwrite an
unmanaged `config.toml` or `AGENTS.md`, and it refuses to mutate a managed file
whose digest has drifted.

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
