#!/usr/bin/env python3
"""Create conservative, Codex-native artifact skeletons without dependencies."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn
from urllib.parse import ParseResult, parse_qsl, urlparse

NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
AGENT_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*\Z")
ENV_NAME_PATTERN = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
SEMVER_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
HOOK_EVENTS = (
    "SessionStart",
    "SubagentStart",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "UserPromptSubmit",
    "SubagentStop",
    "Stop",
)
PERMISSION_PROFILES = {
    "read-only": (":read-only", "on-request"),
    "workspace": (":workspace", "on-request"),
    "danger-full-access": (":danger-full-access", "never"),
}
MAX_TEXT_BYTES = 1024 * 1024
DARWIN_SYSTEM_ALIASES = {Path("/etc"), Path("/tmp"), Path("/var")}


class CreationError(Exception):
    """A stable user-facing creation failure."""


def fail(message: str) -> NoReturn:
    raise CreationError(message)


def absolute_path(raw_path: str) -> Path:
    expanded = Path(raw_path).expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    lexical = Path(os.path.abspath(expanded))
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            if sys.platform == "darwin" and current in DARWIN_SYSTEM_ALIASES:
                continue
            fail(f"output path contains a symlink component: {current}")
    return lexical.resolve(strict=False)


def reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            fail(f"path contains a symlink component: {current}")


def ensure_directory(path: Path) -> None:
    reject_symlink_components(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"cannot create directory {path}: {exc}")
    try:
        info = path.lstat()
    except OSError as exc:
        fail(f"cannot inspect directory {path}: {exc}")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail(f"output path must be a real directory: {path}")


def validate_name(name: str, label: str = "name") -> str:
    if len(name) > 64 or NAME_PATTERN.fullmatch(name) is None:
        fail(f"{label} must be lowercase hyphen-case and at most 64 characters")
    return name


def validate_agent_name(name: str) -> str:
    if len(name) > 64 or AGENT_NAME_PATTERN.fullmatch(name) is None:
        fail(
            "agent name must use lowercase ASCII words separated by hyphens or underscores and be at most 64 characters"
        )
    return name


def validate_description(description: str) -> str:
    value = description.strip()
    if not value or len(value) > 1024 or any(ord(char) < 32 for char in value):
        fail("--description must be one non-empty printable line of at most 1024 characters")
    return value


def display_name(name: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_]", name))


def short_description(description: str) -> str:
    candidate = description.rstrip(".").strip() or "artifact"
    if len(candidate) < 25:
        candidate = f"Create and validate Codex {candidate.lower()}"
    if len(candidate) > 64:
        candidate = candidate[:61].rstrip() + "..."
    if not 25 <= len(candidate) <= 64:
        fail("cannot derive a 25-to-64-character short description")
    return candidate


def validate_required_line(value: str, flag: str) -> str:
    candidate = value.strip()
    if not candidate or any(ord(character) < 32 for character in candidate):
        fail(f"{flag} must be a non-empty printable line")
    return candidate


def parse_absolute_url(raw_url: str, flag: str) -> ParseResult:
    try:
        parsed = urlparse(raw_url)
        # Accessing hostname validates malformed bracketed hosts.
        parsed.hostname
    except ValueError:
        fail(f"{flag} must be a valid absolute URL")
    return parsed


def preflight_targets(paths: list[Path], force: bool) -> None:
    for path in paths:
        reject_symlink_components(path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            fail(f"refusing to replace non-regular path: {path}")
        if not force:
            fail(f"artifact already exists: {path}; pass --force to replace it")


def write_bytes(path: Path, content: bytes) -> None:
    if not content or len(content) > MAX_TEXT_BYTES:
        fail(f"generated artifact has an invalid size: {path}")
    ensure_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_text(path: Path, content: str) -> None:
    if not content.endswith("\n") or "\r" in content:
        fail(f"generated text must be LF-terminated: {path}")
    write_bytes(path, content.encode("utf-8"))


def write_json(path: Path, value: dict[str, Any]) -> None:
    write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_skill(args: argparse.Namespace, output: Path, name: str, description: str) -> list[Path]:
    skill_root = output / name
    targets = [skill_root / "SKILL.md", skill_root / "agents" / "openai.yaml"]
    preflight_targets(targets, args.force)
    title = display_name(name)
    skill = f"""---
name: {name}
description: {yaml_string(description)}
---

# {title}

Use this workflow only when the request matches the description above.

1. Confirm the requested inputs, output location, and acceptance criteria.
2. Perform the task using native Codex surfaces and repository-local conventions.
3. Validate structure, behavior, and safety before reporting completion.
4. Return the created paths, checks run, and any remaining runtime-only verification.
"""
    metadata = f"""interface:
  display_name: {yaml_string(title)}
  short_description: {yaml_string(short_description(description))}
  default_prompt: {yaml_string(f"Use ${name} to {description[0].lower() + description[1:]}")}
"""
    write_text(targets[0], skill)
    write_text(targets[1], metadata)
    return targets


def create_plugin(
    args: argparse.Namespace, output: Path, name: str, description: str
) -> list[Path]:
    if SEMVER_PATTERN.fullmatch(args.version) is None:
        fail("--version must be strict SemVer")
    author = validate_required_line(args.author, "--author")
    license_name = validate_required_line(args.license, "--license")
    category = validate_required_line(args.category, "--category")
    plugin_root = output / name
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    preflight_targets([manifest_path], args.force)
    ensure_directory(plugin_root / "skills")
    title = display_name(name)
    manifest: dict[str, Any] = {
        "name": name,
        "version": args.version,
        "description": description,
        "author": {"name": author},
        "license": license_name,
        "keywords": ["codex", "plugin"],
        "skills": "./skills/",
        "interface": {
            "displayName": title,
            "shortDescription": short_description(description),
            "longDescription": description,
            "developerName": author,
            "category": category,
            "capabilities": [],
            "defaultPrompt": f"Use {title} for this task.",
        },
    }
    if args.repository:
        parsed = parse_absolute_url(args.repository, "--repository")
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            fail("--repository must be an absolute HTTPS URL without embedded credentials")
        manifest["repository"] = args.repository
        manifest["homepage"] = args.repository
    write_json(manifest_path, manifest)
    return [manifest_path]


def validate_plugin_path(raw_path: str) -> str:
    if not raw_path.startswith("./"):
        fail("--plugin-path must start with ./")
    if "\\" in raw_path:
        fail("--plugin-path must use forward slashes")
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".."} for part in pure.parts):
        fail("--plugin-path must stay inside the marketplace root")
    return raw_path.rstrip("/")


def create_marketplace(
    args: argparse.Namespace, output: Path, name: str, description: str
) -> list[Path]:
    if not args.plugin_name:
        fail("marketplace creation requires --plugin-name")
    plugin_name = validate_name(args.plugin_name, "--plugin-name")
    plugin_path = validate_plugin_path(args.plugin_path or f"./plugins/{plugin_name}")
    category = validate_required_line(args.category, "--category")
    manifest_path = output / ".agents" / "plugins" / "marketplace.json"
    preflight_targets([manifest_path], args.force)
    marketplace = {
        "name": name,
        "interface": {"displayName": display_name(name)},
        "plugins": [
            {
                "name": plugin_name,
                "source": {"source": "local", "path": plugin_path},
                "policy": {
                    "installation": args.install_policy,
                    "authentication": args.auth_policy,
                },
                "category": category,
            }
        ],
    }
    write_json(manifest_path, marketplace)
    return [manifest_path]


def create_agent(args: argparse.Namespace, output: Path, name: str, description: str) -> list[Path]:
    target = output / f"{name.replace('_', '-')}.toml"
    preflight_targets([target], args.force)
    instructions = (
        f"You are the {display_name(name)} specialist.\n"
        f"Your responsibility is: {description}\n"
        "Stay within that responsibility, cite concrete evidence, and return concise findings."
    )
    content = (
        f"name = {toml_string(name)}\n"
        f"description = {toml_string(description)}\n"
        f"sandbox_mode = {toml_string(args.sandbox_mode)}\n"
        'developer_instructions = """\n'
        f"{instructions}\n"
        '"""\n'
    )
    write_text(target, content)
    return [target]


def validate_local_hook_references(output: Path, command_tokens: list[str]) -> None:
    references: list[str] = []
    for token in command_tokens:
        normalized = token.replace("\\", "/")
        plugin_prefix = next(
            (
                prefix
                for prefix in ("${PLUGIN_ROOT}/", "$PLUGIN_ROOT/")
                if normalized.startswith(prefix)
            ),
            None,
        )
        if plugin_prefix is not None:
            reference = normalized[len(plugin_prefix) :]
            if not reference:
                fail("PLUGIN_ROOT hook commands must name a plugin-local file")
            references.append(reference)
        elif normalized.startswith(("./", "../")):
            references.append(normalized)
    if not references:
        return
    if output.name != "hooks":
        fail("relative hook commands require --output to name the plugin hooks directory")
    plugin_root = output.parent
    for reference in references:
        candidate = Path(os.path.abspath(plugin_root / reference))
        try:
            contained = os.path.commonpath((str(plugin_root), str(candidate))) == str(plugin_root)
        except ValueError:
            contained = False
        if not contained:
            fail(f"relative hook command escapes the plugin root: {reference}")
        reject_symlink_components(candidate)
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            fail(f"relative hook command does not exist: {reference}")
        except OSError as exc:
            fail(f"cannot inspect relative hook command {reference}: {exc}")
        if not stat.S_ISREG(info.st_mode):
            fail(f"relative hook command must be a regular non-symlink file: {reference}")


def create_hook(args: argparse.Namespace, output: Path, name: str, description: str) -> list[Path]:
    if not args.command or not args.command.strip():
        fail("hook creation requires --command")
    try:
        command_tokens = shlex.split(args.command)
    except ValueError:
        fail("--command must use valid shell quoting")
    if not command_tokens:
        fail("hook creation requires --command")
    validate_local_hook_references(output, command_tokens)
    if not 1 <= args.timeout <= 3600:
        fail("--timeout must be an integer from 1 to 3600")
    target = output / "hooks.json"
    preflight_targets([target], args.force)
    payload = {
        "hooks": {
            args.event: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": args.command,
                            "timeout": args.timeout,
                            "statusMessage": description,
                        }
                    ]
                }
            ]
        }
    }
    write_json(target, payload)
    return [target]


def create_mcp(args: argparse.Namespace, output: Path, name: str, description: str) -> list[Path]:
    target = output / ".mcp.json"
    preflight_targets([target], args.force)
    server: dict[str, Any]
    if args.transport == "stdio":
        if not args.command or not args.command.strip() or args.url:
            fail("stdio MCP creation requires --command and forbids --url")
        if any(not value for value in args.arg):
            fail("--arg values must be non-empty strings")
        server = {"command": args.command}
        if args.arg:
            server["args"] = args.arg
        if args.env_var:
            invalid = [value for value in args.env_var if ENV_NAME_PATTERN.fullmatch(value) is None]
            if invalid:
                fail(f"invalid --env-var names: {invalid}")
            server["env_vars"] = args.env_var
    else:
        if not args.url or args.command:
            fail("HTTP MCP creation requires --url and forbids --command")
        parsed = parse_absolute_url(args.url, "--url")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or (parsed.scheme == "http" and parsed.hostname not in local_hosts)
        ):
            fail("--url must use HTTPS, except for credential-free loopback HTTP")
        for query_key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
            if re.search(r"token|secret|key|password", query_key, re.IGNORECASE) and query_value:
                fail("--url must not carry credentials in its query")
        server = {"url": args.url}
        if args.auth:
            server["auth"] = args.auth
        if args.bearer_token_env_var:
            if ENV_NAME_PATTERN.fullmatch(args.bearer_token_env_var) is None:
                fail("--bearer-token-env-var must be an uppercase environment variable name")
            server["bearer_token_env_var"] = args.bearer_token_env_var
    write_json(target, {name: server})
    return [target]


def create_app(args: argparse.Namespace, output: Path, name: str, description: str) -> list[Path]:
    if not args.app_id or re.fullmatch(r"plugin_asdk_app_[A-Za-z0-9]+", args.app_id) is None:
        fail("app creation requires a valid --app-id beginning with plugin_asdk_app_")
    category = validate_required_line(args.category, "--category")
    target = output / ".app.json"
    preflight_targets([target], args.force)
    write_json(target, {"apps": {name: {"id": args.app_id, "category": category}}})
    return [target]


def create_config(
    args: argparse.Namespace, output: Path, name: str, description: str
) -> list[Path]:
    target = output / "config.toml"
    preflight_targets([target], args.force)
    default_permissions, approval_policy = PERMISSION_PROFILES[args.permission_profile]
    content = (
        f"default_permissions = {toml_string(default_permissions)}\n"
        f"approval_policy = {toml_string(approval_policy)}\n"
        "\n[features]\n"
        "hooks = true\n"
        "multi_agent = true\n"
    )
    write_text(target, content)
    return [target]


def create_instructions(
    args: argparse.Namespace, output: Path, name: str, description: str
) -> list[Path]:
    target = output / "AGENTS.md"
    preflight_targets([target], args.force)
    content = f"""# {display_name(name)} Instructions

## Purpose

{description}

## Rules

- Follow the user's scope and the closest repository instructions.
- Prefer native Codex surfaces and current source-backed contracts.
- Do not expose secrets, weaken safety boundaries, or hide validation failures.
- Keep implementation, verification, and documentation consistent.

## Verification

Run the checks appropriate to every changed artifact and report exact commands.
"""
    write_text(target, content)
    return [target]


def create_rule(args: argparse.Namespace, output: Path, name: str, description: str) -> list[Path]:
    if not args.prefix:
        fail("rule creation requires at least one --prefix token")
    if any(
        not token or any(character.isspace() or not character.isprintable() for character in token)
        for token in args.prefix
    ):
        fail("--prefix tokens must be non-empty printable strings without whitespace")
    target = output / f"{name}.rules"
    preflight_targets([target], args.force)
    pattern = json.dumps(args.prefix, ensure_ascii=False)
    match_cases = [shlex.join(args.prefix)]
    wrong_first = "nddev-negative-example"
    if args.prefix[0] == wrong_first:
        wrong_first = "nddev-negative-example-alt"
    not_match_cases = [shlex.join([wrong_first])]
    if len(args.prefix) > 1:
        not_match_cases.append(shlex.join(args.prefix[:-1]))
    content = f"""prefix_rule(
    pattern = {pattern},
    decision = {json.dumps(args.decision)},
    justification = {json.dumps(description, ensure_ascii=False)},
    match = {json.dumps(match_cases, ensure_ascii=False)},
    not_match = {json.dumps(not_match_cases, ensure_ascii=False)},
)
"""
    write_text(target, content)
    return [target]


CREATORS = {
    "skill": create_skill,
    "plugin": create_plugin,
    "marketplace": create_marketplace,
    "agent": create_agent,
    "hook": create_hook,
    "mcp": create_mcp,
    "app": create_app,
    "config": create_config,
    "instructions": create_instructions,
    "rule": create_rule,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a conservative native Codex artifact skeleton."
    )
    parser.add_argument("kind", choices=tuple(CREATORS))
    parser.add_argument("--output", required=True, help="Parent/root output directory")
    parser.add_argument("--name", required=True, help="Lowercase hyphen-case artifact name")
    parser.add_argument("--description", required=True, help="One-line artifact responsibility")
    parser.add_argument("--force", action="store_true", help="Replace regular files explicitly")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable result")
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--author", default="Local developer")
    parser.add_argument("--license", default="MIT")
    parser.add_argument("--repository")
    parser.add_argument("--category", default="Developer Tools")
    parser.add_argument("--plugin-name")
    parser.add_argument("--plugin-path")
    parser.add_argument(
        "--install-policy",
        choices=("AVAILABLE", "INSTALLED_BY_DEFAULT", "NOT_AVAILABLE"),
        default="AVAILABLE",
    )
    parser.add_argument("--auth-policy", choices=("ON_INSTALL", "ON_USE"), default="ON_INSTALL")
    parser.add_argument(
        "--sandbox-mode",
        choices=("read-only", "workspace-write", "danger-full-access"),
        default="read-only",
    )
    parser.add_argument("--event", choices=HOOK_EVENTS, default="SessionStart")
    parser.add_argument("--command")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--url")
    parser.add_argument("--arg", action="append", default=[])
    parser.add_argument("--env-var", action="append", default=[])
    parser.add_argument("--auth", choices=("oauth", "chatgpt"))
    parser.add_argument("--bearer-token-env-var")
    parser.add_argument("--app-id")
    parser.add_argument(
        "--permission-profile", choices=tuple(PERMISSION_PROFILES), default="read-only"
    )
    parser.add_argument("--prefix", nargs="+")
    parser.add_argument("--decision", choices=("allow", "prompt", "forbidden"), default="prompt")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        name = validate_agent_name(args.name) if args.kind == "agent" else validate_name(args.name)
        description = validate_description(args.description)
        output = absolute_path(args.output)
        ensure_directory(output)
        paths = CREATORS[args.kind](args, output, name, description)
    except CreationError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    result = {"ok": True, "kind": args.kind, "paths": [str(path) for path in paths]}
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        for path in paths:
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
