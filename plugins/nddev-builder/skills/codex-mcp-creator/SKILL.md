---
name: codex-mcp-creator
description: Create or revise native Codex MCP definitions for stdio or HTTP servers with complete transport metadata, clear authentication, and pinned launchers. Use when exposing tools through a plugin or config.
---

# Codex MCP Creator

Resolve every ../../scripts and ../../references path relative to this SKILL.md directory, never the caller working directory.

Define an MCP server at the correct ownership surface with reproducible startup and no embedded credentials.

## Workflow

1. Choose the source surface first: plugin .mcp.json for bundled servers or config TOML for runtime-owned servers.
2. Read ../../references/codex-artifact-contracts.md for the current schema of that surface.
3. Inspect options and generate a plugin MCP definition when appropriate:

       python3 ../../scripts/create_codex_artifact.py mcp --help
       python3 ../../scripts/create_codex_artifact.py mcp --output . --name example-mcp --description "Expose a focused MCP tool surface" --command node --arg ./mcp/server.mjs

4. Preserve complete transport metadata. For stdio, specify command, argv, cwd, and environment references; for HTTP, specify URL and supported authentication metadata.
5. Pin package or binary versions when a launcher resolves mutable dependencies.
6. Keep tokens and credentials in approved environment or OAuth flows, never in source.
7. Validate:

       python3 ../../scripts/check_codex_artifact.py mcp .mcp.json

8. Start or connect in an isolated environment, list tools, and exercise one read-only tool when possible.

## Quality bar

- Server names are stable and unique within their scope.
- Paths and cwd are portable; argv is explicit; startup failures are visible.
- Disabled specialist definitions retain full transport metadata.
- Network and authentication requirements are documented and least-privilege.
