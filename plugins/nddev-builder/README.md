# NDDev Builder

`nddev-builder` is a Codex-native authoring and review plugin for creating
small, portable, testable Codex artifacts. It combines focused workflow skills
with dependency-free generators and deterministic checkers. The plugin does
not ship custom slash commands, and it does not bundle custom-agent TOML:
Codex workflows are skills, while custom agents remain standalone files under
`.codex/agents/` or `$CODEX_HOME/agents/`.

The artifact contracts used by every workflow are documented in
[`references/codex-artifact-contracts.md`](references/codex-artifact-contracts.md).

## Skill inventory

The plugin exposes exactly 28 skills: eleven creator/checker pairs, one
cross-artifact reviewer, and five workflow/lifecycle skills.

| Artifact | Creator | Checker |
| --- | --- | --- |
| Skill | `codex-skill-creator` | `codex-skill-checker` |
| Plugin | `codex-plugin-creator` | `codex-plugin-checker` |
| Marketplace | `codex-marketplace-creator` | `codex-marketplace-checker` |
| Custom agent TOML | `codex-agent-creator` | `codex-agent-checker` |
| Lifecycle hook | `codex-hook-creator` | `codex-hook-checker` |
| MCP server definition | `codex-mcp-creator` | `codex-mcp-checker` |
| App mapping | `codex-app-creator` | `codex-app-checker` |
| Config and permission profile | `codex-config-creator` | `codex-config-checker` |
| `AGENTS.md` instructions | `codex-instructions-creator` | `codex-instructions-checker` |
| Execpolicy rule | `codex-rule-creator` | `codex-rule-checker` |
| Managed requirements | `codex-requirements-creator` | `codex-requirements-checker` |
| Cross-artifact review | `codex-artifact-reviewer` | — |

The five workflow/lifecycle skills orchestrate the artifacts above into a
complete build cycle:

| Workflow | Skill |
| --- | --- |
| Orientation and routing | `codex-builder-orientation` |
| Scaffold a whole plugin | `codex-plugin-scaffolder` |
| Local dev-mode test loop | `codex-plugin-devtest` |
| Version, catalog, and publish | `codex-plugin-publish` |
| Whole-bundle release review | `codex-release-review` |

Invoke a workflow explicitly with its `$skill-name`, or describe the artifact
task and let Codex route from the skill description. Creation skills generate
conservative skeletons; checker skills never rewrite an artifact unless the
user separately requests a repair.

## Generator and checker

Run the scripts from the plugin root or address them by absolute path. Every
generator requires a lowercase hyphen-case `--name`, a one-line
`--description`, and an output parent/root. Agent names additionally accept the
underscore-separated identifiers used by official Codex examples; their output
filenames remain hyphenated. Existing files are preserved unless `--force` is
explicit. Every creation plan is staged completely before commit, writes
owner-only regular files through anchored no-follow directory descriptors, and
restores all prior bytes and modes if any multi-file commit fails.

```bash
python3 scripts/create_codex_artifact.py skill \
  --output "$PWD/.agents/skills" \
  --name repository-review \
  --description "Review repository changes for correctness and missing validation."

python3 scripts/check_codex_artifact.py skill \
  "$PWD/.agents/skills/repository-review"
```

Supported generator/checker kinds are `skill`, `plugin`, `marketplace`,
`agent`, `hook`, `mcp`, `app`, `config`, `instructions`, `rule`, and
`requirements`. The `marketplace` checker accepts `local`, `url`, `git-subdir`,
and `npm` plugin sources and also discovers a sibling `api_marketplace.json`
catalog. Run each
script with `--help` before using kind-specific options such as `--app-id`,
`--transport`, `--event`, or `--prefix`. Use `--json` when another tool needs a
stable machine-readable result.

The generator and non-TOML checks support the module's Python 3.10 floor.
Complete `agent` and `config` TOML validation requires Python 3.11 or newer;
on Python 3.10 the checker fails explicitly instead of returning an unchecked
PASS.

Generated output is a starting point, not proof of product quality. Complete
the workflow-specific content, run the matching checker, then perform the
runtime check named in the artifact contract. Static scans are fail-closed on
unreadable or raced paths and share bounded entry and byte budgets; a static
PASS never substitutes for runtime discovery or behavior.

## Install boundary

This marketplace is independent of the `safe` and `full-auto` setup identities
managed by `nddev-codex-app`. Registering or installing `nddev-builder` must not
change `default_permissions`, `approval_policy`, `sandbox_mode`, or any
`[permissions]` table. Setup switching owns Codex defaults; the marketplace
only makes authoring workflows available.

Use the exact target-owned CLI for registration and inspection. The local
marketplace root is the `nddev-codex-app` repository root containing
`.agents/plugins/marketplace.json`:

```bash
CODEX_HOME=/absolute/path/to/codex-home \
  /absolute/path/to/codex-home/bin/codex plugin marketplace add \
  /absolute/path/to/nddev-codex-app

CODEX_HOME=/absolute/path/to/codex-home \
  /absolute/path/to/codex-home/bin/codex plugin marketplace list

CODEX_HOME=/absolute/path/to/codex-home \
  /absolute/path/to/codex-home/bin/codex plugin add \
  nddev-builder@nddev-builder --json
```

The stable CLI surface covers marketplace registration and plugin install.
Use the ChatGPT desktop plugin directory for cross-surface behavioral testing.
The app-server `plugin/list`, `plugin/read`, `plugin/install`, and
`plugin/uninstall` methods are still marked under development and must not be
treated as a stable production automation API. See [Build plugins](https://learn.chatgpt.com/docs/build-plugins#add-a-marketplace-from-the-cli)
and the [app-server API overview](https://learn.chatgpt.com/docs/app-server#api-overview).

## Development validation

From the `nddev-builder` plugin root, validate the catalog boundary, plugin,
skills, and scripts without touching live user state:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/create_codex_artifact.py --help >/dev/null
PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_codex_artifact.py --help >/dev/null
PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_codex_artifact.py plugin "$PWD"
PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_codex_artifact.py marketplace \
  "$(cd ../.. && pwd)/.agents/plugins/marketplace.json"
```

Exercise generated artifacts only with an isolated `HOME`, `CODEX_HOME`, and
temporary project. Runtime checks may require the pinned target-owned Codex CLI,
ChatGPT authentication, hook trust review, MCP credentials, or a developer-mode
app. Those prerequisites must be reported as runtime evidence, never replaced
with a fake green result.

The public module contains runtime implementation and documentation. Complete
regression, release, fixture, and benchmark coverage remains in the private
`nddev-harnesses` validation slice.
