---
name: codex-hook-creator
description: Create or revise native Codex hooks.json events and command handlers with strict inputs, timeouts, portability, and failure behavior. Use when automating lifecycle checks around Codex sessions or tool calls.
---

# Codex Hook Creator

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Create the smallest hook that enforces a real lifecycle invariant without competing with another owner.

## Workflow

1. Define the event, matcher, input contract, intended side effect, timeout, and failure policy.
2. Read ../../references/codex-artifact-contracts.md for supported hook events and schema.
3. Choose the active-config or plugin-default surface. For a plugin, create the handler first, then generate the interoperable default with a stable `PLUGIN_ROOT` reference:

       python3 ../../scripts/create_codex_artifact.py hook --help
       python3 ../../scripts/create_codex_artifact.py hook --output hooks --name lifecycle-hooks --description "Run bounded lifecycle checks" --command 'sh ${PLUGIN_ROOT}/hooks/session-start.sh'

4. Implement handlers as separate deterministic scripts when logic exceeds a simple command.
5. Resolve plugin-local files through `PLUGIN_ROOT`. For project hooks, resolve from the Git root because commands execute from the session CWD. Quote arguments, bound execution time, and write diagnostics to stderr.
6. Never embed secrets or silently swallow failures. Ensure only one hook owns an ordered lifecycle such as Stop.
7. Validate:

       python3 ../../scripts/check_codex_artifact.py hook hooks/hooks.json

8. Exercise valid input, malformed input, handler failure, and timeout in an isolated environment.

## Quality bar

- Events and matchers are supported and as narrow as possible.
- Commands use portable entry points and referenced files exist.
- Hook output is machine-readable where required and does not corrupt protocol streams.
- Side effects are documented, idempotent where possible, and never touch unrelated live state.
