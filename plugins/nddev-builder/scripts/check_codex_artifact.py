#!/usr/bin/env python3
"""Validate Codex-native artifacts without writes, network, or dependencies."""

from __future__ import annotations

import argparse
import ast
import errno
import json
import os
import re
import shlex
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import parse_qsl, urlparse

try:  # tomllib is part of the standard library starting with Python 3.11.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10.
    tomllib = None  # type: ignore[assignment]


KINDS = (
    "skill",
    "plugin",
    "marketplace",
    "agent",
    "hook",
    "mcp",
    "app",
    "config",
    "instructions",
    "rule",
    "requirements",
    "all",
)
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
SEMVER_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}\Z")
APP_ID_RE = re.compile(r"plugin_asdk_app_[A-Za-z0-9]+\Z")

MAX_TEXT_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_INSTRUCTIONS_BYTES = 32 * 1024
MAX_DISCOVERED_ARTIFACTS = 4096
MAX_TRAVERSED_ENTRIES = 32768
MAX_INSPECTED_BYTES = 64 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024

HOOK_EVENTS = {
    "SessionStart",
    "SessionEnd",
    "SubagentStart",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "UserPromptSubmit",
    "SubagentStop",
    "Stop",
}
HOOK_HANDLER_KEYS = {
    "type",
    "command",
    "commandWindows",
    "timeout",
    "statusMessage",
    "async",
}

PLUGIN_KEYS = {
    "id",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "skills",
    "hooks",
    "mcpServers",
    "apps",
    "interface",
}
PLUGIN_INTERFACE_KEYS = {
    "displayName",
    "shortDescription",
    "longDescription",
    "developerName",
    "category",
    "capabilities",
    "websiteURL",
    "privacyPolicyURL",
    "termsOfServiceURL",
    "defaultPrompt",
    "default_prompt",
    "brandColor",
    "composerIcon",
    "logo",
    "logoDark",
    "screenshots",
}
PLUGIN_INTERFACE_REQUIRED = {
    "displayName",
    "shortDescription",
    "longDescription",
    "developerName",
    "category",
}

MCP_SERVER_KEYS = {
    "type",
    "command",
    "args",
    "env",
    "env_vars",
    "cwd",
    "url",
    "auth",
    "bearer_token_env_var",
    "http_headers",
    "headers",
    "env_http_headers",
    "startup_timeout_sec",
    "tool_timeout_sec",
    "enabled",
    "required",
    "enabled_tools",
    "disabled_tools",
    "default_tools_approval_mode",
    "tools",
    "scopes",
    "oauth",
    "oauth_resource",
    "experimental_environment",
    "environment_id",
    "name",
    "startup_timeout_ms",
    "supports_parallel_tool_calls",
    "title",
    "description",
    "icons",
}
SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-api-key",
    "api-key",
}

AGENT_FIELDS = {
    "name",
    "description",
    "developer_instructions",
    "nickname_candidates",
}

MCP_APPROVAL_MODES = {"auto", "prompt", "writes", "approve"}
# The Codex 0.145.0 reasoning-effort ladder (protocol/src/openai_models.rs).
# config.schema.json types `ReasoningEffort` as a non-empty string, and
# `model_reasoning_effort`/`plan_mode_reasoning_effort` share the same type as an
# agent role's effort, so accept the full known ladder for all three (a stricter
# config set wrongly rejected `max`/`ultra`).
AGENT_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
}
CONFIG_REASONING_EFFORTS = AGENT_REASONING_EFFORTS
PLAN_REASONING_EFFORTS = AGENT_REASONING_EFFORTS

DEPRECATED_CONFIG_KEYS = {
    "codex_hooks",
    "plugin_hooks",
    "experimental_instructions_file",
    "background_terminal_timeout",
    "experimental_use_unified_exec_tool",
    "use_legacy_landlock",
}
LEGACY_WEB_FEATURE_KEYS = {
    "features.web_search",
    "features.web_search_request",
    "features.web_search_cached",
}

TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cfg",
    ".conf",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsonc",
    ".md",
    ".mjs",
    ".py",
    ".rs",
    ".rules",
    ".sh",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
BINARY_SUFFIXES = {".gif", ".ico", ".jpeg", ".jpg", ".pdf", ".png", ".webp"}
SKIP_DISCOVERY_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}

PLACEHOLDER_RE = re.compile(
    r"(?:\[\s*TO" + r"DO\b|<\s*TO" + r"DO\b|\b(?:TO" + r"DO|FIX" + r"ME|T" + r"BD)\s*[:_-])",
    re.IGNORECASE,
)
HIGH_CONFIDENCE_SECRET_RES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE" + r" KEY-----"),
    re.compile(r"AK" + r"IA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_" + r"pat_[A-Za-z0-9_]{20,}"),
)
OPENAI_SECRET_RE = re.compile(r"sk-" + r"[A-Za-z0-9_-]{20,}")
OPENAI_EXAMPLE_SECRET_MARKERS = (
    "example",
    "sample",
    "dummy",
    "placeholder",
    "redacted",
    "test",
)
OPENAI_EXAMPLE_SECRET_SUFFIX_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?im)^[ \t]*(?:[\"']?)(api[_-]?key|client[_-]?secret|access[_-]?token|"
    r"bearer[_-]?token|password|passwd)(?:[\"']?)[ \t]*[:=][ \t]*"
    r"[\"']?([^\"'\s,}#]{8,})"
)

# Exact, deliberately boring values accepted in examples. Substring matching
# is unsafe here: a live value such as `real-example-token-...` must not become
# exempt merely because it contains a documentation word.
SAFE_SECRET_PLACEHOLDERS = {
    "changeme",
    "dummy",
    "example",
    "placeholder",
    "redacted",
    "sample",
    "test",
    "your-api-key",
    "your-client-secret",
    "your-password",
    "your-token",
    "your_api_key",
    "your_client_secret",
    "your_password",
    "your_token",
    "xxx",
    "xxxxxxxx",
}

# Top-level properties from the official Codex 0.145.0 ConfigToml schema:
# https://github.com/openai/codex/blob/rust-v0.145.0/codex-rs/core/config.schema.json
# Validation is intentionally top-level only. Named tables such as
# mcp_servers.<name>, permissions.<name>, plugins, marketplaces, projects, and
# agents have dynamic keys and are validated by Codex or focused checkers.
CONFIG_TOP_LEVEL_KEYS = {
    "agents",
    "allow_login_shell",
    "analytics",
    "approval_policy",
    "approvals_reviewer",
    "apps",
    "apps_mcp_product_sku",
    "audio",
    "auto_review",
    "background_terminal_max_timeout",
    "chatgpt_base_url",
    "check_for_update_on_startup",
    "cli_auth_credentials_store",
    "compact_prompt",
    "debug",
    "default_permissions",
    "desktop",
    "developer_instructions",
    "disable_paste_burst",
    "experimental_compact_prompt_file",
    "experimental_realtime_start_instructions",
    "experimental_realtime_webrtc_call_base_url",
    "experimental_realtime_ws_backend_prompt",
    "experimental_realtime_ws_base_url",
    "experimental_realtime_ws_model",
    "experimental_realtime_ws_startup_context",
    "experimental_thread_config_endpoint",
    "experimental_thread_store",
    "experimental_use_unified_exec_tool",
    "features",
    "feedback",
    "file_opener",
    "forced_chatgpt_workspace_id",
    "forced_login_method",
    "ghost_snapshot",
    "hide_agent_reasoning",
    "history",
    "hooks",
    "include_apps_instructions",
    "include_collaboration_mode_instructions",
    "include_environment_context",
    "include_permissions_instructions",
    "instructions",
    "log_dir",
    "marketplaces",
    "mcp_oauth_callback_port",
    "mcp_oauth_callback_url",
    "mcp_oauth_credentials_store",
    "mcp_servers",
    "memories",
    "model",
    "model_auto_compact_token_limit",
    "model_auto_compact_token_limit_scope",
    "model_catalog_json",
    "model_context_window",
    "model_instructions_file",
    "model_provider",
    "model_providers",
    "model_reasoning_effort",
    "model_reasoning_summary",
    "model_verbosity",
    "notice",
    "notify",
    "openai_base_url",
    "orchestrator",
    "oss_provider",
    "otel",
    "permissions",
    "personality",
    "plan_mode_reasoning_effort",
    "plugins",
    "profile",
    "profiles",
    "project_doc_fallback_filenames",
    "project_doc_max_bytes",
    "project_root_markers",
    "projects",
    "realtime",
    "review_model",
    "sandbox_mode",
    "sandbox_workspace_write",
    "service_tier",
    "shell_environment_policy",
    "show_raw_agent_reasoning",
    "skills",
    "sqlite_home",
    "suppress_unstable_features_warning",
    "tool_output_token_limit",
    "tool_suggest",
    "tools",
    "tui",
    "web_search",
    "windows",
}


class DuplicateJsonKey(ValueError):
    """Raised when a JSON object contains a duplicate member name."""


@dataclass(order=True, frozen=True)
class Finding:
    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "path": self.path}


@dataclass
class ScanBudget:
    """One deterministic resource budget shared by a validation operation."""

    max_entries: int = MAX_TRAVERSED_ENTRIES
    max_bytes: int = MAX_INSPECTED_BYTES
    traversed_entries: int = 0
    inspected_bytes: int = 0
    exhausted: bool = False

    def consume_entry(self, report: ArtifactReport, path: Path) -> bool:
        if self.exhausted:
            return False
        if self.traversed_entries >= self.max_entries:
            self.exhausted = True
            report.error(
                "scan-entry-limit",
                path,
                f"validation exceeds the aggregate limit of {self.max_entries} entries",
            )
            return False
        self.traversed_entries += 1
        return True

    def can_inspect(self, report: ArtifactReport, path: Path, size: int) -> bool:
        if self.exhausted:
            return False
        remaining = self.max_bytes - self.inspected_bytes
        if size > remaining:
            self.exhausted = True
            report.error(
                "scan-byte-limit",
                path,
                f"validation exceeds the aggregate limit of {self.max_bytes} inspected bytes",
            )
            return False
        return True

    def consume_bytes(self, report: ArtifactReport, path: Path, size: int) -> bool:
        if not self.can_inspect(report, path, size):
            return False
        self.inspected_bytes += size
        return True


@dataclass
class ArtifactReport:
    kind: str
    path: Path
    budget: ScanBudget = field(default_factory=ScanBudget, repr=False)
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    checked_files: set[str] = field(default_factory=set, repr=False)

    def error(self, code: str, path: Path, message: str) -> None:
        self.errors.append(Finding(_display_path(path), code, message))

    def warning(self, code: str, path: Path, message: str) -> None:
        self.warnings.append(Finding(_display_path(path), code, message))

    def to_dict(self) -> dict[str, Any]:
        return {
            "errors": [finding.to_dict() for finding in sorted(set(self.errors))],
            "kind": self.kind,
            "path": _display_path(self.path),
            "status": "FAIL" if self.errors else "PASS",
            "warnings": [finding.to_dict() for finding in sorted(set(self.warnings))],
        }


def _display_path(path: Path) -> str:
    return str(path)


def _absolute_path(raw: str | Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(path))


def _is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except OSError:
        return False


def _is_macos_system_alias(path: Path, info: os.stat_result) -> bool:
    """Allow only Apple's root-owned aliases into `/private`."""
    if sys.platform != "darwin" or info.st_uid != 0:
        return False
    expected_targets = {
        Path("/etc"): "private/etc",
        Path("/tmp"): "private/tmp",
        Path("/var"): "private/var",
    }
    expected = expected_targets.get(path)
    if expected is None:
        return False
    try:
        return os.readlink(path) == expected
    except OSError:
        return False


def _symlink_component(path: Path) -> Path | None:
    """Return the first unsafe symlink in the lexical path chain."""
    absolute = _absolute_path(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            # No later lexical component can exist before this one exists.
            return None
        except OSError:
            return current
        if stat.S_ISLNK(info.st_mode) and not _is_macos_system_alias(current, info):
            return current
    return None


def _symlink_beneath(base: Path, candidate: Path) -> Path | None:
    absolute_base = _absolute_path(base)
    absolute_candidate = _absolute_path(candidate)
    if not _lexically_contained(absolute_base, absolute_candidate):
        return absolute_candidate
    current = absolute_base
    try:
        relative_parts = absolute_candidate.relative_to(absolute_base).parts
    except ValueError:
        return absolute_candidate
    for part in relative_parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return current
        if stat.S_ISLNK(info.st_mode):
            return current
    return None


def _preflight_path(report: ArtifactReport, path: Path, *, directory: bool | None) -> bool:
    component = _symlink_component(path)
    if component is not None:
        report.error("symlink", component, "symlink inputs and path components are not allowed")
        return False
    try:
        info = path.lstat()
    except FileNotFoundError:
        report.error("missing", path, "artifact does not exist")
        return False
    except OSError as exc:
        report.error("unreadable", path, f"cannot inspect artifact: {exc}")
        return False
    if stat.S_ISLNK(info.st_mode):
        report.error("symlink", path, "symlink artifacts are not allowed")
        return False
    if directory is True and not stat.S_ISDIR(info.st_mode):
        report.error("type", path, "artifact must be a directory")
        return False
    if directory is False and not stat.S_ISREG(info.st_mode):
        report.error("type", path, "artifact must be a regular file")
        return False
    return True


def _walk_onerror(report: ArtifactReport, root: Path) -> Callable[[OSError], None]:
    def record(exc: OSError) -> None:
        raw_path = exc.filename if isinstance(exc.filename, (str, bytes)) else str(root)
        if isinstance(raw_path, bytes):
            raw_path = os.fsdecode(raw_path)
        report.error("unreadable", _absolute_path(raw_path), "cannot traverse directory")

    return record


def _walk_real_files(report: ArtifactReport, root: Path) -> Iterable[Path]:
    if not report.budget.consume_entry(report, root):
        return
    for current_raw, directories, files in os.walk(
        root,
        followlinks=False,
        onerror=_walk_onerror(report, root),
    ):
        current = Path(current_raw)
        kept_directories: list[str] = []
        for name in sorted(directories):
            candidate = current / name
            if not report.budget.consume_entry(report, candidate):
                directories[:] = []
                return
            if _is_symlink(candidate):
                report.error("symlink", candidate, "symlink artifacts are not allowed")
            else:
                kept_directories.append(name)
        directories[:] = kept_directories
        for name in sorted(files):
            candidate = current / name
            if not report.budget.consume_entry(report, candidate):
                directories[:] = []
                return
            if _is_symlink(candidate):
                report.error("symlink", candidate, "symlink artifacts are not allowed")
                continue
            try:
                info = candidate.lstat()
            except OSError as exc:
                report.error("unreadable", candidate, f"cannot inspect artifact file: {exc}")
                continue
            if not stat.S_ISREG(info.st_mode):
                report.error("type", candidate, "artifact members must be regular files")
                continue
            if info.st_size > MAX_ARTIFACT_BYTES:
                report.error(
                    "oversized",
                    candidate,
                    f"artifact file exceeds {MAX_ARTIFACT_BYTES} bytes",
                )
                continue
            yield candidate


def _looks_textual(path: Path) -> bool:
    return (
        path.suffix.lower() in TEXT_SUFFIXES
        or path.name in {"AGENTS.md", "SKILL.md", "LICENSE", "Dockerfile", "Makefile"}
        or path.name.startswith(".")
        and path.suffix.lower() in TEXT_SUFFIXES
    )


def _fd_path(path: Path) -> Path:
    """Map only Apple's root-owned compatibility aliases to their real path."""
    absolute = _absolute_path(path)
    if sys.platform != "darwin" or len(absolute.parts) < 2:
        return absolute
    alias = Path(absolute.anchor) / absolute.parts[1]
    try:
        info = alias.lstat()
    except OSError:
        return absolute
    if not stat.S_ISLNK(info.st_mode) or not _is_macos_system_alias(alias, info):
        return absolute
    return Path("/private") / Path(*absolute.parts[1:])


def _open_parent_nofollow(path: Path) -> tuple[int, str]:
    """Open a file's parent one lexical directory at a time without symlinks."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory or os.open not in os.supports_dir_fd:
        raise OSError(errno.ENOTSUP, "anchored no-follow file access is unavailable", str(path))

    absolute = _fd_path(path)
    if len(absolute.parts) < 2:
        raise OSError(errno.EINVAL, "file path has no leaf component", str(path))
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:-1]:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
    except BaseException:
        os.close(current_fd)
        raise
    return current_fd, absolute.name


def _file_identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _file_state(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        *_file_identity(info),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_error(report: ArtifactReport, path: Path, exc: OSError) -> None:
    if exc.errno == errno.ELOOP:
        report.error("symlink", path, "symlink artifacts and path components are not allowed")
    else:
        report.error("unreadable", path, f"cannot safely read file: {exc}")


def _read_bytes(report: ArtifactReport, path: Path, limit: int) -> bytes | None:
    """Read one stable regular file through an anchored, no-follow descriptor."""
    if report.budget.exhausted:
        return None
    if _symlink_component(path) is not None:
        report.error("symlink", path, "symlink artifacts are not allowed")
        return None

    parent_fd: int | None = None
    file_fd: int | None = None
    try:
        parent_fd, leaf = _open_parent_nofollow(path)
        parent_before = os.fstat(parent_fd)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        file_fd = os.open(leaf, flags, dir_fd=parent_fd)
        before = os.fstat(file_fd)
    except OSError as exc:
        _read_error(report, path, exc)
        return None
    try:
        if not stat.S_ISREG(before.st_mode):
            report.error("type", path, "artifact must be a regular file")
            return None
        if before.st_size > limit:
            report.error("oversized", path, f"file exceeds {limit} bytes")
            return None
        # Charge the declared extent before reading. This intentionally
        # over-counts truncated/erroring files rather than letting failed reads
        # bypass the aggregate byte ceiling.
        if not report.budget.consume_bytes(report, path, before.st_size):
            return None

        data = bytearray()
        while len(data) < before.st_size:
            chunk = os.read(file_fd, min(READ_CHUNK_BYTES, before.st_size - len(data)))
            if not chunk:
                report.error("changed-during-read", path, "file changed while it was being read")
                return None
            data.extend(chunk)
        growth_probe = os.read(file_fd, 1)
        if growth_probe:
            report.budget.consume_bytes(report, path, len(growth_probe))
            report.error("changed-during-read", path, "file grew while it was being read")
            return None

        after = os.fstat(file_fd)
        post_parent_fd, post_leaf = _open_parent_nofollow(path)
        try:
            post_parent = os.fstat(post_parent_fd)
            bound = os.stat(post_leaf, dir_fd=post_parent_fd, follow_symlinks=False)
        finally:
            os.close(post_parent_fd)
        if _file_identity(post_parent) != _file_identity(parent_before):
            report.error("changed-during-read", path, "file parent binding changed while reading")
            return None
        if stat.S_ISLNK(bound.st_mode):
            report.error("symlink", path, "file was replaced by a symlink while reading")
            return None
        if _file_state(after) != _file_state(before):
            report.error("changed-during-read", path, "file changed while it was being read")
            return None
        if _file_state(bound) != _file_state(before):
            report.error("changed-during-read", path, "file path binding changed while reading")
            return None
        return bytes(data)
    except OSError as exc:
        _read_error(report, path, exc)
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _safe_secret_value(value: str) -> bool:
    normalized = value.strip().strip("\"'")
    lowered = normalized.lower()
    if not normalized:
        return True
    if re.fullmatch(r"\$[A-Z_][A-Z0-9_]*", normalized):
        return True
    if re.fullmatch(r"\$\{[A-Z_][A-Z0-9_]*\}", normalized):
        return True
    if re.fullmatch(r"env:[A-Z_][A-Z0-9_]*", normalized):
        return True
    if re.fullmatch(r"(?:secret|vault)://[A-Za-z0-9][A-Za-z0-9._/-]*", normalized):
        return True
    if re.fullmatch(r"<[A-Za-z0-9][A-Za-z0-9_-]*>", normalized):
        return True
    if _is_openai_example_secret(normalized):
        return True
    return lowered in SAFE_SECRET_PLACEHOLDERS


def _is_openai_example_secret(value: str) -> bool:
    lowered = value.lower()
    if not lowered.startswith("sk-"):
        return False
    tail = lowered[3:]
    for marker in OPENAI_EXAMPLE_SECRET_MARKERS:
        if tail == marker:
            return True
        if not tail.startswith(marker):
            continue
        suffix = tail[len(marker) :]
        return (
            len(suffix) >= 2
            and suffix[0] in "-_"
            and all(character in OPENAI_EXAMPLE_SECRET_SUFFIX_CHARS for character in suffix)
        )
    return False


def _scan_text(report: ArtifactReport, path: Path, text: str) -> None:
    if PLACEHOLDER_RE.search(text):
        report.error("placeholder", path, "unresolved placeholder marker found")
    for pattern in HIGH_CONFIDENCE_SECRET_RES:
        if pattern.search(text):
            report.error("inline-secret", path, "high-confidence inline secret found")
            break
    for match in OPENAI_SECRET_RE.finditer(text):
        if not _is_openai_example_secret(match.group(0)):
            report.error("inline-secret", path, "high-confidence inline secret found")
            break
    for match in SECRET_ASSIGNMENT_RE.finditer(text):
        if not _safe_secret_value(match.group(2)):
            report.error(
                "inline-secret",
                path,
                f"inline value for secret-like field `{match.group(1)}` is not allowed",
            )
            break


def _read_text(
    report: ArtifactReport,
    path: Path,
    *,
    limit: int = MAX_TEXT_BYTES,
    scan: bool = True,
) -> str | None:
    key = _display_path(path)
    data = _read_bytes(report, path, limit)
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        report.error("utf8", path, "text artifact must be valid UTF-8")
        return None
    if scan and key not in report.checked_files:
        report.checked_files.add(key)
        _scan_text(report, path, text)
    return text


def _scan_tree(report: ArtifactReport, root: Path) -> None:
    for path in _walk_real_files(report, root):
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        if _looks_textual(path):
            _read_text(report, path)
            continue
        data = _read_bytes(report, path, MAX_TEXT_BYTES)
        if data is None or b"\x00" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            report.error("utf8", path, "non-binary artifact must be valid UTF-8")
            continue
        key = _display_path(path)
        if key not in report.checked_files:
            report.checked_files.add(key)
            _scan_text(report, path, text)


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(key)
        result[key] = value
    return result


def _load_json(report: ArtifactReport, path: Path) -> dict[str, Any] | None:
    text = _read_text(report, path)
    if text is None:
        return None
    try:
        payload = json.loads(text, object_pairs_hook=_json_pairs)
    except DuplicateJsonKey as exc:
        report.error("json-duplicate-key", path, f"duplicate JSON member `{exc}`")
        return None
    except json.JSONDecodeError as exc:
        report.error("json-syntax", path, f"invalid JSON at line {exc.lineno}, column {exc.colno}")
        return None
    if not isinstance(payload, dict):
        report.error("json-shape", path, "JSON artifact must contain an object")
        return None
    return payload


def _load_toml(report: ArtifactReport, path: Path) -> tuple[dict[str, Any] | None, str | None]:
    text = _read_text(report, path)
    if text is None:
        return None, None
    if tomllib is None:
        report.error(
            "toml-parser-unavailable",
            path,
            "complete TOML validation requires Python 3.11 or newer; refusing an unchecked PASS",
        )
        return None, text
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        report.error("toml-syntax", path, f"invalid TOML: {exc}")
        return None, text
    if not isinstance(payload, dict):
        report.error("toml-shape", path, "TOML artifact must contain a table")
        return None, text
    return payload, text


def _unknown_keys(
    report: ArtifactReport,
    path: Path,
    payload: dict[str, Any],
    allowed: set[str],
    label: str,
) -> None:
    for key in sorted(set(payload) - allowed):
        report.error("unknown-field", path, f"unsupported {label} field `{key}`")


def _require_string(
    report: ArtifactReport,
    path: Path,
    payload: dict[str, Any],
    key: str,
    label: str,
) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        report.error("required-field", path, f"{label} `{key}` must be a non-empty string")
        return None
    return value.strip()


def _validate_name(report: ArtifactReport, path: Path, name: Any, label: str = "name") -> bool:
    if not isinstance(name, str) or len(name) > 64 or NAME_RE.fullmatch(name) is None:
        report.error(
            "name",
            path,
            f"{label} must be lowercase hyphen-case and at most 64 characters",
        )
        return False
    return True


def _validate_https_url(
    report: ArtifactReport,
    path: Path,
    value: Any,
    label: str,
    *,
    optional: bool = True,
) -> None:
    if value is None and optional:
        return
    parsed = urlparse(value) if isinstance(value, str) else None
    if parsed is None or parsed.scheme != "https" or not parsed.netloc or parsed.username:
        report.error("url", path, f"{label} must be an absolute HTTPS URL without credentials")


def _lexically_contained(base: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath(
            (str(_absolute_path(base)), str(_absolute_path(candidate)))
        ) == str(_absolute_path(base))
    except ValueError:
        return False


def _contract_path(
    report: ArtifactReport,
    manifest_path: Path,
    base: Path,
    raw: Any,
    label: str,
    *,
    expected_kind: str | None = None,
    require_exists: bool = True,
) -> Path | None:
    if not isinstance(raw, str) or not raw.startswith("./") or "\\" in raw:
        report.error("path", manifest_path, f"{label} must be a relative path beginning with `./`")
        return None
    pure = PurePosixPath(raw)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".."} for part in pure.parts):
        report.error("path", manifest_path, f"{label} must stay inside the artifact root")
        return None
    candidate = _absolute_path(base / pure.as_posix())
    if not _lexically_contained(base, candidate):
        report.error("path", manifest_path, f"{label} escapes the artifact root")
        return None
    component = _symlink_beneath(base, candidate)
    if component is not None:
        report.error("symlink", component, f"{label} must not traverse a symlink")
        return None
    if not candidate.exists():
        if require_exists:
            report.error("missing-reference", manifest_path, f"{label} points to a missing path")
        return candidate
    if expected_kind == "file" and not candidate.is_file():
        report.error("path-type", manifest_path, f"{label} must point to a regular file")
    if expected_kind == "directory" and not candidate.is_dir():
        report.error("path-type", manifest_path, f"{label} must point to a directory")
    return candidate


YAML_INVALID = object()


def _strip_yaml_comment(raw: str) -> str | None:
    """Strip a YAML comment while proving that quotes are balanced."""
    first = len(raw) - len(raw.lstrip())
    if first >= len(raw) or raw[first] not in {'"', "'"}:
        for index, character in enumerate(raw):
            if character == "#" and (index == 0 or raw[index - 1].isspace()):
                return raw[:index].rstrip()
        return raw.rstrip()

    quote = raw[first]
    escaped = False
    index = first + 1
    while index < len(raw):
        character = raw[index]
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                break
        elif character == quote:
            if index + 1 < len(raw) and raw[index + 1] == quote:
                index += 1
            else:
                break
        index += 1
    else:
        return None

    remainder = raw[index + 1 :]
    stripped_remainder = remainder.lstrip()
    if stripped_remainder and not stripped_remainder.startswith("#"):
        return None
    return raw[: index + 1].rstrip()


def _yaml_scalar(raw: str) -> Any:
    """Parse the scalar subset used by Codex skill metadata."""
    uncommented = _strip_yaml_comment(raw)
    if uncommented is None:
        return YAML_INVALID
    value = uncommented.strip()
    if not value:
        return YAML_INVALID
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return YAML_INVALID
        return parsed if isinstance(parsed, str) else YAML_INVALID
    if value.startswith("'"):
        return value[1:-1].replace("''", "'")
    if value in {"|", ">"}:
        return value
    if value.startswith(("[", "{", "&", "*", "!", "@", "`")):
        return YAML_INVALID
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    if re.fullmatch(r"[-+]?(?:0|[1-9][0-9]*)", value):
        try:
            return int(value)
        except ValueError:  # pragma: no cover - guarded by the expression.
            return YAML_INVALID
    if re.fullmatch(
        r"[-+]?(?:(?:0|[1-9][0-9]*)\.[0-9]+|(?:0|[1-9][0-9]*)[eE][-+]?[0-9]+)",
        value,
    ):
        try:
            return float(value)
        except ValueError:  # pragma: no cover - guarded by the expression.
            return YAML_INVALID
    return value


def _parse_skill_frontmatter(
    report: ArtifactReport, path: Path, text: str
) -> tuple[dict[str, Any], str] | None:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        report.error("frontmatter", path, "SKILL.md must start with YAML frontmatter")
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        report.error("frontmatter", path, "SKILL.md frontmatter is not closed")
        return None
    values: dict[str, Any] = {}
    index = 1
    while index < end:
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if line.startswith((" ", "\t")) or ":" not in line:
            report.error("frontmatter", path, f"unsupported frontmatter syntax on line {index + 1}")
            return None
        key, raw = line.split(":", 1)
        key = key.strip()
        if key in values:
            report.error("frontmatter", path, f"duplicate frontmatter field `{key}`")
            return None
        scalar = _yaml_scalar(raw)
        if scalar in {"|", ">"}:
            block: list[str] = []
            index += 1
            while index < end and (not lines[index] or lines[index].startswith((" ", "\t"))):
                block.append(lines[index].lstrip())
                index += 1
            scalar = ("\n" if scalar == "|" else " ").join(block).strip()
            values[key] = scalar
            continue
        if scalar is YAML_INVALID:
            report.error("frontmatter", path, f"frontmatter field `{key}` must be a scalar string")
            return None
        values[key] = scalar
        index += 1
    return values, "\n".join(lines[end + 1 :]).strip()


def _yaml_mapping_entry(content: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*):(.*)", content)
    if match is None:
        return None
    return match.group(1), match.group(2)


def _openai_yaml_lines(
    report: ArtifactReport, path: Path, text: str
) -> list[tuple[int, str, int]] | None:
    if "\t" in text:
        report.error("yaml-syntax", path, "openai.yaml must use spaces, not tabs")
        return None
    parsed: list[tuple[int, str, int]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indentation = len(line) - len(line.lstrip(" "))
        if indentation % 2:
            report.error(
                "yaml-syntax",
                path,
                f"openai.yaml indentation on line {index} must use two-space steps",
            )
            return None
        parsed.append((indentation, line[indentation:], index))
    return parsed


def _parse_openai_yaml(report: ArtifactReport, path: Path, text: str) -> dict[str, Any] | None:
    """Parse the documented openai.yaml mapping/list subset without PyYAML."""
    lines = _openai_yaml_lines(report, path, text)
    if lines is None:
        return None
    payload: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        indentation, content, line_number = lines[index]
        if indentation != 0:
            report.error("yaml-syntax", path, f"unexpected indentation on line {line_number}")
            return None
        entry = _yaml_mapping_entry(content)
        if entry is None:
            report.error("yaml-syntax", path, f"invalid mapping on line {line_number}")
            return None
        section, raw = entry
        if section in payload:
            report.error("yaml-syntax", path, f"duplicate top-level field `{section}`")
            return None
        section_value = _strip_yaml_comment(raw)
        if section_value is None or section_value != "":
            report.error(
                "yaml-syntax",
                path,
                f"openai.yaml section `{section}` must contain a nested mapping",
            )
            return None
        index += 1
        block_start = index
        while index < len(lines) and lines[index][0] > 0:
            index += 1
        block = lines[block_start:index]
        if section in {"interface", "policy"}:
            mapping: dict[str, Any] = {}
            for child_indent, child_content, child_line in block:
                if child_indent != 2 or child_content.startswith("-"):
                    report.error(
                        "yaml-syntax",
                        path,
                        f"section `{section}` has unsupported structure on line {child_line}",
                    )
                    return None
                child_entry = _yaml_mapping_entry(child_content)
                if child_entry is None:
                    report.error("yaml-syntax", path, f"invalid mapping on line {child_line}")
                    return None
                key, child_raw = child_entry
                if key in mapping:
                    report.error("yaml-syntax", path, f"duplicate `{section}.{key}` field")
                    return None
                value = _yaml_scalar(child_raw)
                if value is YAML_INVALID:
                    report.error(
                        "yaml-syntax",
                        path,
                        f"`{section}.{key}` has an invalid scalar on line {child_line}",
                    )
                    return None
                mapping[key] = value
            payload[section] = mapping
            continue
        if section != "dependencies":
            payload[section] = {}
            continue

        dependencies: dict[str, Any] = {}
        if not block:
            payload[section] = dependencies
            continue
        tool_header = block[0]
        tool_entry = _yaml_mapping_entry(tool_header[1])
        if (
            tool_header[0] != 2
            or tool_entry is None
            or tool_entry[0] != "tools"
            or _strip_yaml_comment(tool_entry[1]) != ""
        ):
            report.error(
                "yaml-syntax",
                path,
                f"dependencies must contain a nested `tools` list (line {tool_header[2]})",
            )
            return None
        tools: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for child_indent, child_content, child_line in block[1:]:
            if child_indent == 4 and child_content.startswith("- "):
                first_entry = _yaml_mapping_entry(child_content[2:])
                if first_entry is None:
                    report.error(
                        "yaml-syntax", path, f"invalid dependency tool on line {child_line}"
                    )
                    return None
                current = {}
                tools.append(current)
                key, child_raw = first_entry
            elif child_indent == 6 and current is not None:
                continuation = _yaml_mapping_entry(child_content)
                if continuation is None:
                    report.error("yaml-syntax", path, f"invalid mapping on line {child_line}")
                    return None
                key, child_raw = continuation
            else:
                report.error(
                    "yaml-syntax",
                    path,
                    f"dependencies.tools has unsupported structure on line {child_line}",
                )
                return None
            if key in current:
                report.error("yaml-syntax", path, f"duplicate dependencies.tools field `{key}`")
                return None
            value = _yaml_scalar(child_raw)
            if value is YAML_INVALID:
                report.error(
                    "yaml-syntax",
                    path,
                    f"dependencies.tools.{key} has an invalid scalar on line {child_line}",
                )
                return None
            current[key] = value
        dependencies["tools"] = tools
        payload[section] = dependencies
    return payload


MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^\s)>]+)>?")
SKILL_RESOURCE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"((?:\.\.?/)*(?:scripts|references|assets|agents)/"
    r"(?:[A-Za-z0-9._@+-]+/)*[A-Za-z0-9][A-Za-z0-9._@+-]*)"
    r"(?=$|[\s`'\"\)\],;:!?])"
)


def _skill_reference_boundary(root: Path) -> Path:
    """Keep normal resources in a skill and plugin-shared resources in a plugin."""
    if root.parent.name == "skills":
        plugin_root = root.parent.parent
        if (plugin_root / ".codex-plugin" / "plugin.json").is_file():
            return plugin_root
    return root


def _skill_local_references(text: str) -> set[str]:
    references = {match.group(1) for match in MARKDOWN_LINK_RE.finditer(text)}
    references.update(match.group(1) for match in SKILL_RESOURCE_PATH_RE.finditer(text))
    return references


def _validate_skill_references(
    report: ArtifactReport, root: Path, skill_path: Path, text: str
) -> None:
    boundary = _absolute_path(_skill_reference_boundary(root))
    for raw in sorted(_skill_local_references(text)):
        reference = raw.split("#", 1)[0].split("?", 1)[0].rstrip(".,;:!?")
        if not reference or reference.startswith("#"):
            continue
        parsed = urlparse(reference)
        if parsed.scheme or reference.startswith("//"):
            continue
        if "%" in reference or "\\" in reference or Path(reference).is_absolute():
            report.error(
                "skill-reference",
                skill_path,
                f"local skill reference `{raw}` must be an unencoded relative path",
            )
            continue
        candidate = _absolute_path(skill_path.parent / reference)
        if not _lexically_contained(boundary, candidate):
            report.error(
                "skill-reference",
                skill_path,
                f"local skill reference `{raw}` escapes its artifact boundary",
            )
            continue
        component = _symlink_component(candidate)
        if component is not None:
            report.error("symlink", component, f"local skill reference `{raw}` traverses a symlink")
            continue
        if not candidate.exists():
            report.error(
                "missing-reference",
                skill_path,
                f"local skill reference `{raw}` does not exist",
            )
        elif not (candidate.is_file() or candidate.is_dir()):
            report.error(
                "path-type",
                candidate,
                f"local skill reference `{raw}` must target a regular file or directory",
            )


def _validate_openai_metadata(
    report: ArtifactReport,
    path: Path,
    root: Path,
    payload: dict[str, Any],
    skill_name: str | None,
) -> None:
    _unknown_keys(report, path, payload, {"interface", "policy", "dependencies"}, "openai.yaml")

    interface = payload.get("interface")
    if interface is not None:
        if not isinstance(interface, dict):
            report.error("type", path, "openai.yaml interface must be a mapping")
        else:
            allowed_interface = {
                "display_name",
                "short_description",
                "icon_small",
                "icon_large",
                "brand_color",
                "default_prompt",
            }
            _unknown_keys(report, path, interface, allowed_interface, "openai.yaml interface")
            for key in ("display_name", "short_description", "default_prompt"):
                value = interface.get(key)
                if value is not None and (not isinstance(value, str) or not value.strip()):
                    report.error(
                        "type", path, f"openai.yaml interface.{key} must be a non-empty string"
                    )
            short = interface.get("short_description")
            if isinstance(short, str) and short.strip() and not 25 <= len(short) <= 64:
                report.error(
                    "short-description",
                    path,
                    "interface.short_description must be 25 to 64 characters",
                )
            prompt = interface.get("default_prompt")
            if (
                isinstance(prompt, str)
                and prompt.strip()
                and isinstance(skill_name, str)
                and f"${skill_name}" not in prompt
            ):
                report.error(
                    "default-prompt",
                    path,
                    f"interface.default_prompt must explicitly mention `${skill_name}`",
                )
            brand_color = interface.get("brand_color")
            if brand_color is not None and (
                not isinstance(brand_color, str) or HEX_COLOR_RE.fullmatch(brand_color) is None
            ):
                report.error("color", path, "interface.brand_color must use `#RRGGBB`")
            for icon_key in ("icon_small", "icon_large"):
                if icon_key in interface:
                    _contract_path(
                        report,
                        path,
                        root,
                        interface[icon_key],
                        f"interface.{icon_key}",
                        expected_kind="file",
                    )

    policy = payload.get("policy")
    if policy is not None:
        if not isinstance(policy, dict):
            report.error("type", path, "openai.yaml policy must be a mapping")
        else:
            _unknown_keys(
                report,
                path,
                policy,
                {"allow_implicit_invocation"},
                "openai.yaml policy",
            )
            implicit = policy.get("allow_implicit_invocation")
            if implicit is not None and not isinstance(implicit, bool):
                report.error(
                    "type",
                    path,
                    "openai.yaml policy.allow_implicit_invocation must be a boolean",
                )

    dependencies = payload.get("dependencies")
    if dependencies is None:
        return
    if not isinstance(dependencies, dict):
        report.error("type", path, "openai.yaml dependencies must be a mapping")
        return
    _unknown_keys(report, path, dependencies, {"tools"}, "openai.yaml dependencies")
    tools = dependencies.get("tools")
    if not isinstance(tools, list) or not tools:
        report.error("type", path, "openai.yaml dependencies.tools must be a non-empty list")
        return
    allowed_tool_fields = {"type", "value", "description", "transport", "url"}
    for index, tool in enumerate(tools):
        label = f"dependencies.tools[{index}]"
        if not isinstance(tool, dict):
            report.error("type", path, f"openai.yaml {label} must be a mapping")
            continue
        _unknown_keys(report, path, tool, allowed_tool_fields, f"openai.yaml {label}")
        if tool.get("type") != "mcp":
            report.error("type", path, f"openai.yaml {label}.type must be `mcp`")
        value = tool.get("value")
        if not isinstance(value, str) or not value.strip():
            report.error(
                "required-field", path, f"openai.yaml {label}.value must be a non-empty string"
            )
        description = tool.get("description")
        if description is not None and (
            not isinstance(description, str) or not description.strip()
        ):
            report.error("type", path, f"openai.yaml {label}.description must be a string")
        transport = tool.get("transport")
        if transport is not None and transport not in {"stdio", "streamable_http"}:
            report.error(
                "type",
                path,
                f"openai.yaml {label}.transport must be `stdio` or `streamable_http`",
            )
        if "url" in tool:
            _validate_https_url(
                report, path, tool["url"], f"openai.yaml {label}.url", optional=False
            )
            if transport is not None and transport != "streamable_http":
                report.error(
                    "type",
                    path,
                    f"openai.yaml {label}.url requires `transport: streamable_http`",
                )


def _validate_skill_root(root: Path, *, budget: ScanBudget | None = None) -> ArtifactReport:
    report = ArtifactReport("skill", root, budget=budget or ScanBudget())
    if not _preflight_path(report, root, directory=True):
        return report
    _scan_tree(report, root)
    skill_path = root / "SKILL.md"
    if not skill_path.is_file() or _is_symlink(skill_path):
        report.error("missing", skill_path, "skill requires a real SKILL.md file")
        return report
    text = _read_text(report, skill_path)
    if text is None:
        return report
    parsed = _parse_skill_frontmatter(report, skill_path, text)
    if parsed is None:
        return report
    frontmatter, body = parsed
    for key in sorted(set(frontmatter) - {"name", "description"}):
        report.error("unknown-field", skill_path, f"unsupported skill frontmatter field `{key}`")
    name = frontmatter.get("name")
    if _validate_name(report, skill_path, name, "skill name") and name != root.name:
        report.error("name-mismatch", skill_path, "skill name must match its directory name")
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        report.error(
            "description",
            skill_path,
            "skill description must be non-empty and at most 1024 characters",
        )
    if not body:
        report.error("body", skill_path, "SKILL.md requires non-empty workflow instructions")
    _validate_skill_references(report, root, skill_path, text)
    metadata_path = root / "agents" / "openai.yaml"
    if metadata_path.exists():
        if not _preflight_path(report, metadata_path, directory=False):
            return report
        metadata_text = _read_text(report, metadata_path)
        if metadata_text is None:
            return report
        metadata = _parse_openai_yaml(report, metadata_path, metadata_text)
        if metadata is not None:
            _validate_openai_metadata(
                report,
                metadata_path,
                root,
                metadata,
                name if isinstance(name, str) else None,
            )
    return report


def _validate_app_file(
    path: Path,
    report: ArtifactReport | None = None,
    *,
    budget: ScanBudget | None = None,
) -> ArtifactReport:
    own_report = report or ArtifactReport("app", path, budget=budget or ScanBudget())
    if not _preflight_path(own_report, path, directory=False):
        return own_report
    payload = _load_json(own_report, path)
    if payload is None:
        return own_report
    _unknown_keys(own_report, path, payload, {"apps"}, "app manifest")
    apps = payload.get("apps")
    if not isinstance(apps, dict) or not apps:
        own_report.error("required-field", path, "app manifest requires a non-empty `apps` map")
        return own_report
    for name, entry in sorted(apps.items()):
        if not isinstance(name, str) or IDENTIFIER_RE.fullmatch(name) is None:
            own_report.error("name", path, f"app key `{name}` is not a valid identifier")
        if not isinstance(entry, dict):
            own_report.error("app-shape", path, f"app `{name}` must be an object")
            continue
        app_id = entry.get("id")
        if not isinstance(app_id, str) or APP_ID_RE.fullmatch(app_id) is None:
            own_report.error(
                "app-id",
                path,
                f"app `{name}` id must begin with `plugin_asdk_app_` and contain only letters or digits after it",
            )
        category = entry.get("category")
        if category is not None and (not isinstance(category, str) or not category.strip()):
            own_report.error("category", path, f"app `{name}` category must be a non-empty string")
        required = entry.get("required")
        if required is not None and not isinstance(required, bool):
            own_report.error("type", path, f"app `{name}` required must be a boolean")
    return own_report


HOOK_SCRIPT_INTERPRETERS = {
    "bash",
    "node",
    "nodejs",
    "perl",
    "powershell",
    "pwsh",
    "python",
    "python3",
    "ruby",
    "sh",
    "zsh",
}


def _explicit_relative_command_path(token: str) -> str | None:
    normalized = token.replace("\\", "/")
    if normalized.startswith("$"):
        return None
    if normalized.startswith(("./", "../")):
        return normalized
    if (
        "/" in normalized
        and not normalized.startswith("/")
        and "://" not in normalized
        and re.match(r"^[A-Za-z]:/", normalized) is None
    ):
        return normalized
    return None


def _plugin_root_command_path(token: str) -> str | None:
    normalized = token.replace("\\", "/")
    for prefix in ("${PLUGIN_ROOT}/", "$PLUGIN_ROOT/"):
        if normalized.startswith(prefix):
            return normalized[len(prefix) :]
    return None


def _hook_command_reference(command: str) -> tuple[str, bool] | None:
    """Return an explicit local hook entry point, not arbitrary output arguments."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return ("", False)
    if not tokens:
        return ("", False)
    executable = tokens[0]
    plugin_reference = _plugin_root_command_path(executable)
    if plugin_reference is not None:
        return (plugin_reference, True)
    executable_reference = _explicit_relative_command_path(executable)
    if executable_reference is not None:
        return (executable_reference, False)
    if Path(executable).name not in HOOK_SCRIPT_INTERPRETERS:
        return None
    for token in tokens[1:]:
        if token == "--":
            continue
        if token in {"-c", "-e", "--eval", "-m"}:
            return None
        if token.startswith("-"):
            continue
        plugin_reference = _plugin_root_command_path(token)
        if plugin_reference is not None:
            return (plugin_reference, True)
        reference = _explicit_relative_command_path(token)
        if reference is not None:
            return (reference, False)
        # The first non-option is an inline command/module or a PATH-resolved
        # entry point. Neither can be proven as a local file statically.
        return None
    return None


def _validate_hook_command(
    report: ArtifactReport,
    manifest_path: Path,
    command: str,
    label: str,
    plugin_root: Path | None,
) -> None:
    reference_details = _hook_command_reference(command)
    if reference_details == ("", False):
        report.error("hook-command", manifest_path, f"`{label}` command has invalid shell syntax")
        return
    if reference_details is None:
        return
    reference, uses_plugin_root = reference_details
    if uses_plugin_root:
        if plugin_root is None:
            report.error(
                "hook-command",
                manifest_path,
                f"`{label}` uses PLUGIN_ROOT outside a plugin hook context",
            )
            return
        base = plugin_root
    else:
        base = plugin_root or (
            manifest_path.parent.parent
            if manifest_path.parent.name in {".codex", "hooks"}
            else manifest_path.parent
        )
    candidate = _absolute_path(base / reference)
    if not _lexically_contained(base, candidate):
        report.error(
            "hook-command",
            manifest_path,
            f"`{label}` local command `{reference}` escapes the hook artifact root",
        )
        return
    component = _symlink_component(candidate)
    if component is not None:
        report.error("symlink", component, f"`{label}` local command traverses a symlink")
        return
    if not candidate.is_file():
        report.error(
            "missing-reference",
            manifest_path,
            f"`{label}` local command `{reference}` does not name an existing regular file",
        )


def _validate_hook_payload(
    payload: dict[str, Any],
    own_report: ArtifactReport,
    path: Path,
    plugin_root: Path | None,
) -> None:
    _unknown_keys(own_report, path, payload, {"hooks"}, "hook manifest")
    events = payload.get("hooks")
    if not isinstance(events, dict) or not events:
        own_report.error("required-field", path, "hook manifest requires a non-empty `hooks` map")
        return
    for event, groups in sorted(events.items()):
        if event not in HOOK_EVENTS:
            own_report.error("hook-event", path, f"unsupported Codex hook event `{event}`")
        if not isinstance(groups, list) or not groups:
            own_report.error("hook-groups", path, f"hook event `{event}` requires a non-empty list")
            continue
        for group_index, group in enumerate(groups):
            label = f"{event}[{group_index}]"
            if not isinstance(group, dict):
                own_report.error("hook-group", path, f"hook group `{label}` must be an object")
                continue
            _unknown_keys(own_report, path, group, {"matcher", "hooks"}, f"hook group `{label}`")
            matcher = group.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                own_report.error("matcher", path, f"hook group `{label}` matcher must be a string")
            handlers = group.get("hooks")
            if not isinstance(handlers, list) or not handlers:
                own_report.error("hook-handlers", path, f"hook group `{label}` requires handlers")
                continue
            for handler_index, handler in enumerate(handlers):
                handler_label = f"{label}.hooks[{handler_index}]"
                if not isinstance(handler, dict):
                    own_report.error("hook-handler", path, f"`{handler_label}` must be an object")
                    continue
                _unknown_keys(
                    own_report,
                    path,
                    handler,
                    HOOK_HANDLER_KEYS,
                    f"hook handler `{handler_label}`",
                )
                handler_type = handler.get("type")
                if handler_type not in {"command", "prompt", "agent"}:
                    own_report.error(
                        "hook-type",
                        path,
                        f"`{handler_label}` type must be `command`, `prompt`, or `agent`",
                    )
                elif handler_type != "command":
                    own_report.warning(
                        "inactive-hook-handler",
                        path,
                        f"`{handler_label}` type `{handler_type}` is parsed but skipped by current Codex",
                    )
                command = handler.get("command")
                if handler_type == "command" and (
                    not isinstance(command, str) or not command.strip()
                ):
                    own_report.error("hook-command", path, f"`{handler_label}` requires a command")
                elif isinstance(command, str) and command.strip():
                    _validate_hook_command(
                        own_report,
                        path,
                        command,
                        handler_label,
                        plugin_root,
                    )
                timeout = handler.get("timeout")
                if timeout is not None and (
                    isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 0
                ):
                    own_report.error(
                        "hook-timeout",
                        path,
                        f"`{handler_label}` timeout must be a nonnegative integer",
                    )
                if "async" in handler and not isinstance(handler["async"], bool):
                    own_report.error("type", path, f"`{handler_label}` async must be a boolean")
                elif handler.get("async") is True:
                    own_report.warning(
                        "inactive-hook-handler",
                        path,
                        f"`{handler_label}` async handler is parsed but skipped by current Codex",
                    )
                status_message = handler.get("statusMessage")
                if status_message is not None and (
                    not isinstance(status_message, str) or not status_message.strip()
                ):
                    own_report.error(
                        "status-message",
                        path,
                        f"`{handler_label}` statusMessage must be a non-empty string",
                    )
                windows_command = handler.get("commandWindows")
                if windows_command is not None:
                    if not isinstance(windows_command, str) or not windows_command.strip():
                        own_report.error(
                            "windows-command",
                            path,
                            f"`{handler_label}` commandWindows must be a non-empty command string",
                        )
                    else:
                        _validate_hook_command(
                            own_report,
                            path,
                            windows_command,
                            f"{handler_label}.commandWindows",
                            plugin_root,
                        )


def _validate_hook_file(
    path: Path,
    report: ArtifactReport | None = None,
    plugin_root: Path | None = None,
    *,
    budget: ScanBudget | None = None,
) -> ArtifactReport:
    own_report = report or ArtifactReport("hook", path, budget=budget or ScanBudget())
    if plugin_root is None and path.parent.name == "hooks":
        plugin_root = path.parent.parent
    if not _preflight_path(own_report, path, directory=False):
        return own_report
    payload = _load_json(own_report, path)
    if payload is not None:
        _validate_hook_payload(payload, own_report, path, plugin_root)
    return own_report


def _validate_string_list(
    report: ArtifactReport,
    path: Path,
    value: Any,
    label: str,
    *,
    allow_empty: bool = True,
) -> bool:
    valid = isinstance(value, list) and all(isinstance(item, str) and item for item in value)
    if valid and not allow_empty and not value:
        valid = False
    if not valid:
        report.error(
            "type",
            path,
            f"{label} must be {'a non-empty' if not allow_empty else 'an'} array of strings",
        )
        return False
    return True


def _validate_env_map(report: ArtifactReport, path: Path, value: Any, label: str) -> None:
    if not isinstance(value, dict):
        report.error("type", path, f"{label} must be an object of string values")
        return
    for key, raw_value in value.items():
        if not isinstance(key, str) or ENV_NAME_RE.fullmatch(key) is None:
            report.error("env-name", path, f"{label} contains invalid environment name `{key}`")
        if not isinstance(raw_value, str):
            report.error("type", path, f"{label}.{key} must be a string")
            continue
        if re.search(
            r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)\Z", key
        ) and not _safe_secret_value(raw_value):
            report.error(
                "inline-secret", path, f"{label}.{key} must reference an environment secret"
            )


def _validate_mcp_env_vars(report: ArtifactReport, path: Path, value: Any, label: str) -> None:
    if not isinstance(value, list):
        report.error(
            "type",
            path,
            f"{label} must be an array of strings or name/source objects",
        )
        return
    for index, entry in enumerate(value):
        entry_label = f"{label}[{index}]"
        if isinstance(entry, str):
            if not entry.strip():
                report.error("type", path, f"{entry_label} must be a non-empty string")
            continue
        if not isinstance(entry, dict):
            report.error("type", path, f"{entry_label} must be a string or object")
            continue
        _unknown_keys(report, path, entry, {"name", "source"}, entry_label)
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            report.error("required-field", path, f"{entry_label}.name must be a string")
        source = entry.get("source")
        if source is not None and source not in {"local", "remote"}:
            report.error("type", path, f"{entry_label}.source must be `local` or `remote`")


def _validate_mcp_tool_policy(
    report: ArtifactReport, path: Path, server_name: str, server: dict[str, Any]
) -> None:
    default_mode = server.get("default_tools_approval_mode")
    if default_mode is not None and default_mode not in MCP_APPROVAL_MODES:
        report.error(
            "mcp-approval-mode",
            path,
            f"MCP server `{server_name}` default_tools_approval_mode is invalid",
        )
    tools = server.get("tools")
    if tools is None:
        return
    if not isinstance(tools, dict):
        report.error("type", path, f"MCP server `{server_name}` tools must be an object")
        return
    for tool_name, policy in sorted(tools.items()):
        if not isinstance(tool_name, str) or not tool_name:
            report.error("name", path, f"MCP server `{server_name}` has an invalid tool name")
        if not isinstance(policy, dict):
            report.error(
                "type",
                path,
                f"MCP server `{server_name}` tool `{tool_name}` policy must be an object",
            )
            continue
        _unknown_keys(
            report,
            path,
            policy,
            {"approval_mode"},
            f"MCP server `{server_name}` tool `{tool_name}`",
        )
        mode = policy.get("approval_mode")
        if mode is not None and mode not in MCP_APPROVAL_MODES:
            report.error(
                "mcp-approval-mode",
                path,
                f"MCP server `{server_name}` tool `{tool_name}` approval_mode is invalid",
            )


def _validate_mcp_servers(report: ArtifactReport, path: Path, servers: Any, base: Path) -> None:
    if not isinstance(servers, dict) or not servers:
        report.error("required-field", path, "MCP manifest requires a non-empty server map")
        return
    for name, server in sorted(servers.items()):
        if not isinstance(name, str) or IDENTIFIER_RE.fullmatch(name) is None:
            report.error("name", path, f"MCP server name `{name}` is invalid")
        if not isinstance(server, dict):
            report.error("mcp-shape", path, f"MCP server `{name}` must be an object")
            continue
        _unknown_keys(report, path, server, MCP_SERVER_KEYS, f"MCP server `{name}`")
        has_command = isinstance(server.get("command"), str) and bool(server["command"].strip())
        has_url = isinstance(server.get("url"), str) and bool(server["url"].strip())
        if has_command == has_url:
            report.error(
                "mcp-transport",
                path,
                f"MCP server `{name}` must define exactly one of `command` (stdio) or `url` (HTTP)",
            )
            continue
        declared_type = server.get("type")
        expected_type = "stdio" if has_command else "http"
        if declared_type is not None and declared_type != expected_type:
            report.error(
                "mcp-transport",
                path,
                f"MCP server `{name}` type must be `{expected_type}` for its transport",
            )
        environment = server.get("experimental_environment")
        if environment is not None and environment not in {"local", "remote"}:
            report.error(
                "mcp-environment",
                path,
                f"MCP server `{name}` experimental_environment must be `local` or `remote`",
            )
        if environment == "remote" and has_url:
            report.error(
                "mcp-environment",
                path,
                f"HTTP MCP server `{name}` cannot use experimental remote placement",
            )
        environment_id = server.get("environment_id")
        if environment_id is not None and (
            not isinstance(environment_id, str) or not environment_id.strip()
        ):
            report.error(
                "type", path, f"MCP server `{name}` environment_id must be a non-empty string"
            )
        _validate_mcp_tool_policy(report, path, name, server)
        if has_command:
            if "args" in server:
                _validate_string_list(report, path, server["args"], f"MCP server `{name}` args")
            if "env" in server:
                _validate_env_map(report, path, server["env"], f"MCP server `{name}` env")
            if "env_vars" in server:
                _validate_mcp_env_vars(
                    report, path, server["env_vars"], f"MCP server `{name}` env_vars"
                )
            if any(
                key in server
                for key in (
                    "auth",
                    "bearer_token_env_var",
                    "http_headers",
                    "headers",
                    "env_http_headers",
                )
            ):
                report.error(
                    "mcp-transport", path, f"stdio MCP server `{name}` contains HTTP-only fields"
                )
        else:
            parsed = urlparse(server["url"])
            local_hosts = {"localhost", "127.0.0.1", "::1"}
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or (parsed.scheme == "http" and parsed.hostname not in local_hosts)
            ):
                report.error(
                    "mcp-url",
                    path,
                    f"HTTP MCP server `{name}` requires HTTPS (HTTP is allowed only for loopback) and no embedded credentials",
                )
            for query_key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
                if (
                    re.search(r"token|secret|key|password", query_key, re.IGNORECASE)
                    and query_value
                ):
                    report.error(
                        "inline-secret",
                        path,
                        f"HTTP MCP server `{name}` URL must not carry credentials in its query",
                    )
            auth = server.get("auth")
            if auth is not None and auth not in {"oauth", "chatgpt"}:
                report.error(
                    "mcp-auth", path, f"HTTP MCP server `{name}` auth must be `oauth` or `chatgpt`"
                )
            for stdio_field in ("args", "env", "env_vars", "cwd"):
                if stdio_field in server:
                    report.error(
                        "mcp-transport",
                        path,
                        f"HTTP MCP server `{name}` contains stdio-only field `{stdio_field}`",
                    )
            bearer_env = server.get("bearer_token_env_var")
            if bearer_env is not None and (
                not isinstance(bearer_env, str) or ENV_NAME_RE.fullmatch(bearer_env) is None
            ):
                report.error(
                    "env-name",
                    path,
                    f"HTTP MCP server `{name}` bearer_token_env_var must be an uppercase environment name",
                )
            for header_field in ("http_headers", "headers"):
                if header_field not in server:
                    continue
                headers = server[header_field]
                if not isinstance(headers, dict) or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in headers.items()
                ):
                    report.error(
                        "type",
                        path,
                        f"HTTP MCP server `{name}` {header_field} must map strings to strings",
                    )
                    continue
                for header_name in headers:
                    if header_name.lower() in SENSITIVE_HEADER_NAMES:
                        report.error(
                            "inline-secret",
                            path,
                            f"HTTP MCP server `{name}` must source sensitive header `{header_name}` from env_http_headers",
                        )
            env_headers = server.get("env_http_headers")
            if env_headers is not None:
                if not isinstance(env_headers, dict):
                    report.error(
                        "type", path, f"HTTP MCP server `{name}` env_http_headers must be an object"
                    )
                else:
                    for header, env_name in env_headers.items():
                        if not isinstance(header, str) or not header.strip():
                            report.error(
                                "header",
                                path,
                                f"HTTP MCP server `{name}` has an invalid header name",
                            )
                        if not isinstance(env_name, str) or ENV_NAME_RE.fullmatch(env_name) is None:
                            report.error(
                                "env-name",
                                path,
                                f"HTTP MCP server `{name}` env_http_headers values must be environment names",
                            )
        for timeout_key in ("startup_timeout_sec", "tool_timeout_sec"):
            if timeout_key in server:
                timeout = server[timeout_key]
                if (
                    isinstance(timeout, bool)
                    or not isinstance(timeout, (int, float))
                    or timeout <= 0
                ):
                    report.error(
                        "timeout",
                        path,
                        f"MCP server `{name}` {timeout_key} must be a positive number",
                    )
        if "startup_timeout_ms" in server:
            timeout_ms = server["startup_timeout_ms"]
            if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 0:
                report.error(
                    "timeout",
                    path,
                    f"MCP server `{name}` startup_timeout_ms must be a nonnegative integer",
                )
        for boolean_key in ("enabled", "required"):
            if boolean_key in server and not isinstance(server[boolean_key], bool):
                report.error("type", path, f"MCP server `{name}` {boolean_key} must be a boolean")
        if "supports_parallel_tool_calls" in server and not isinstance(
            server["supports_parallel_tool_calls"], bool
        ):
            report.error(
                "type",
                path,
                f"MCP server `{name}` supports_parallel_tool_calls must be a boolean",
            )
        for tools_key in ("enabled_tools", "disabled_tools"):
            if tools_key in server:
                _validate_string_list(
                    report, path, server[tools_key], f"MCP server `{name}` {tools_key}"
                )
        if "scopes" in server:
            _validate_string_list(report, path, server["scopes"], f"MCP server `{name}` scopes")
        for string_key in ("name", "oauth_resource", "title", "description"):
            if string_key in server and (
                not isinstance(server[string_key], str) or not server[string_key].strip()
            ):
                report.error(
                    "type",
                    path,
                    f"MCP server `{name}` {string_key} must be a non-empty string",
                )
        cwd = server.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str) or Path(cwd).is_absolute() or ".." in Path(cwd).parts:
                report.error("path", path, f"MCP server `{name}` cwd must stay inside the plugin")
            elif not _lexically_contained(base, base / cwd):
                report.error("path", path, f"MCP server `{name}` cwd escapes the plugin")
        icons = server.get("icons")
        if icons is not None:
            if not isinstance(icons, list):
                report.error("type", path, f"MCP server `{name}` icons must be an array")
            else:
                for index, icon in enumerate(icons):
                    if not isinstance(icon, dict) or not isinstance(icon.get("src"), str):
                        report.error(
                            "icon", path, f"MCP server `{name}` icon {index} requires a string src"
                        )
                        continue
                    _contract_path(
                        report,
                        path,
                        base,
                        icon["src"],
                        f"MCP server `{name}` icon {index}",
                        expected_kind="file",
                    )


def _validate_mcp_file(
    path: Path,
    report: ArtifactReport | None = None,
    *,
    budget: ScanBudget | None = None,
) -> ArtifactReport:
    own_report = report or ArtifactReport("mcp", path, budget=budget or ScanBudget())
    if not _preflight_path(own_report, path, directory=False):
        return own_report
    payload = _load_json(own_report, path)
    if payload is None:
        return own_report
    wrapped_keys = {key for key in ("mcpServers", "mcp_servers") if key in payload}
    if len(wrapped_keys) > 1:
        own_report.error(
            "mcp-shape",
            path,
            "MCP manifest must not declare both `mcpServers` and `mcp_servers` wrappers",
        )
        return own_report
    if wrapped_keys:
        wrapper = next(iter(wrapped_keys))
        _unknown_keys(own_report, path, payload, {wrapper}, "MCP manifest")
        servers = payload.get(wrapper)
    else:
        servers = payload
    _validate_mcp_servers(own_report, path, servers, path.parent)
    return own_report


def _validate_plugin_native_boundaries(report: ArtifactReport, root: Path) -> None:
    """Reject foreign workflow surfaces and standalone agents inside a plugin."""
    for name in ("commands", "agents"):
        candidate = root / name
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            report.error("unreadable", candidate, f"cannot inspect plugin boundary: {exc}")
            continue
        report.error(
            "unsupported-plugin-surface",
            candidate,
            f"plugin root must not bundle `{name}`; Codex workflows are skills and custom agents are standalone TOML",
        )

    if not report.budget.consume_entry(report, root):
        return
    for current_raw, directories, files in os.walk(
        root,
        followlinks=False,
        onerror=_walk_onerror(report, root),
    ):
        current = Path(current_raw)
        kept_directories: list[str] = []
        for name in sorted(directories):
            candidate = current / name
            if not report.budget.consume_entry(report, candidate):
                directories[:] = []
                return
            if _is_symlink(candidate):
                report.error("symlink", candidate, "symlink artifacts are not allowed")
            else:
                kept_directories.append(name)
        directories[:] = kept_directories
        for name in sorted(files):
            candidate = current / name
            if not report.budget.consume_entry(report, candidate):
                directories[:] = []
                return
            if _is_symlink(candidate):
                report.error("symlink", candidate, "symlink artifacts are not allowed")
                continue
            if candidate.suffix.lower() != ".toml":
                continue
            relative = candidate.relative_to(root)
            if "agents" in relative.parts:
                report.error(
                    "unsupported-plugin-surface",
                    candidate,
                    "custom-agent TOML is standalone under CODEX_HOME/agents or .codex/agents and must not be bundled in a plugin",
                )


def _validate_plugin_hooks(
    report: ArtifactReport,
    manifest_path: Path,
    root: Path,
    raw_hooks: Any,
) -> None:
    report.warning(
        "hook-manifest-compatibility",
        manifest_path,
        "runtime accepts a manifest hooks override, but the bundled plugin validator currently requires default discovery",
    )
    entries: list[str] | list[dict[str, Any]]
    if isinstance(raw_hooks, str):
        entries = [raw_hooks]
    elif isinstance(raw_hooks, dict):
        entries = [raw_hooks]
    elif isinstance(raw_hooks, list) and raw_hooks:
        if all(isinstance(entry, str) for entry in raw_hooks):
            entries = raw_hooks
        elif all(isinstance(entry, dict) for entry in raw_hooks):
            entries = raw_hooks
        else:
            report.error(
                "type",
                manifest_path,
                "plugin hooks list must contain only paths or only inline hook objects",
            )
            return
    else:
        report.error(
            "type",
            manifest_path,
            "plugin hooks must be a path, path list, inline object, or inline-object list",
        )
        return

    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            hook_path = _contract_path(
                report,
                manifest_path,
                root,
                entry,
                f"plugin hooks[{index}]",
                expected_kind="file",
            )
            if hook_path is not None and hook_path.is_file():
                _validate_hook_file(hook_path, report, plugin_root=root)
        else:
            _validate_hook_payload(entry, report, manifest_path, root)


def _validate_plugin_root(root: Path, *, budget: ScanBudget | None = None) -> ArtifactReport:
    report = ArtifactReport("plugin", root, budget=budget or ScanBudget())
    if not _preflight_path(report, root, directory=True):
        return report
    _scan_tree(report, root)
    _validate_plugin_native_boundaries(report, root)
    manifest_path = root / ".codex-plugin" / "plugin.json"
    if not manifest_path.is_file() or _is_symlink(manifest_path):
        report.error("missing", manifest_path, "plugin requires `.codex-plugin/plugin.json`")
        return report
    manifest = _load_json(report, manifest_path)
    if manifest is None:
        return report
    _unknown_keys(report, manifest_path, manifest, PLUGIN_KEYS, "plugin manifest")
    name = _require_string(report, manifest_path, manifest, "name", "plugin field")
    if name is not None:
        if _validate_name(report, manifest_path, name, "plugin name") and name != root.name:
            report.error(
                "name-mismatch",
                manifest_path,
                "plugin name must match its directory name",
            )
    version = _require_string(report, manifest_path, manifest, "version", "plugin field")
    if version is not None and SEMVER_RE.fullmatch(version) is None:
        report.error("semver", manifest_path, "plugin version must use strict SemVer")
    _require_string(report, manifest_path, manifest, "description", "plugin field")
    author = manifest.get("author")
    if not isinstance(author, dict):
        report.error("required-field", manifest_path, "plugin author must be an object")
    else:
        _unknown_keys(report, manifest_path, author, {"name", "email", "url"}, "author")
        _require_string(report, manifest_path, author, "name", "author field")
        if "email" in author and (
            not isinstance(author["email"], str) or "@" not in author["email"]
        ):
            report.error("email", manifest_path, "author.email must be a valid non-empty address")
        _validate_https_url(report, manifest_path, author.get("url"), "author.url")
    for url_key in ("homepage", "repository"):
        _validate_https_url(report, manifest_path, manifest.get(url_key), url_key)
    if "license" in manifest and (
        not isinstance(manifest["license"], str) or not manifest["license"].strip()
    ):
        report.error("type", manifest_path, "plugin license must be a non-empty string")
    if "keywords" in manifest:
        _validate_string_list(report, manifest_path, manifest["keywords"], "plugin keywords")

    skills_path: Path | None = None
    if "skills" in manifest:
        skills_path = _contract_path(
            report,
            manifest_path,
            root,
            manifest["skills"],
            "plugin skills",
            expected_kind="directory",
        )
    elif (root / "skills").is_dir():
        skills_path = root / "skills"
    if skills_path is not None and skills_path.is_dir():
        for candidate in sorted(skills_path.iterdir(), key=lambda item: item.name):
            if candidate.name.startswith("."):
                continue
            if _is_symlink(candidate):
                report.error("symlink", candidate, "plugin skills must not be symlinks")
            elif candidate.is_dir():
                child = _validate_skill_root(candidate, budget=report.budget)
                report.errors.extend(child.errors)
                report.warnings.extend(child.warnings)

    if "hooks" in manifest:
        _validate_plugin_hooks(report, manifest_path, root, manifest["hooks"])
    elif (root / "hooks" / "hooks.json").is_file():
        _validate_hook_file(root / "hooks" / "hooks.json", report, plugin_root=root)

    mcp_value = manifest.get("mcpServers")
    if isinstance(mcp_value, str):
        mcp_path = _contract_path(
            report,
            manifest_path,
            root,
            mcp_value,
            "plugin mcpServers",
            expected_kind="file",
        )
        if mcp_path is not None and mcp_path.is_file():
            _validate_mcp_file(mcp_path, report)
    elif isinstance(mcp_value, dict):
        _validate_mcp_servers(report, manifest_path, mcp_value, root)
    elif mcp_value is not None:
        report.error("type", manifest_path, "plugin mcpServers must be a path or object")

    if "apps" in manifest:
        apps_path = _contract_path(
            report,
            manifest_path,
            root,
            manifest["apps"],
            "plugin apps",
            expected_kind="file",
        )
        if apps_path is not None and apps_path.is_file():
            _validate_app_file(apps_path, report)

    interface = manifest.get("interface")
    if not isinstance(interface, dict):
        report.error("required-field", manifest_path, "plugin interface must be an object")
        return report
    _unknown_keys(report, manifest_path, interface, PLUGIN_INTERFACE_KEYS, "plugin interface")
    for key in sorted(PLUGIN_INTERFACE_REQUIRED):
        _require_string(report, manifest_path, interface, key, "plugin interface field")
    capabilities = interface.get("capabilities")
    if capabilities is None:
        report.error("required-field", manifest_path, "plugin interface requires `capabilities`")
    else:
        _validate_string_list(report, manifest_path, capabilities, "interface.capabilities")
    # The Codex runtime deserializes interface.defaultPrompt as
    # Option<Vec<String>>, so a bare string is non-conforming and is rejected at
    # load even though older examples used one. Require the array form.
    prompts = interface.get("defaultPrompt", interface.get("default_prompt"))
    if isinstance(prompts, list):
        if not 1 <= len(prompts) <= 3 or not all(
            isinstance(prompt, str) and prompt.strip() and len(prompt) <= 128 for prompt in prompts
        ):
            report.error(
                "default-prompt",
                manifest_path,
                "plugin default prompts must contain one to three non-empty strings of at most 128 characters",
            )
    elif isinstance(prompts, str):
        report.error(
            "default-prompt",
            manifest_path,
            "plugin interface `defaultPrompt` must be an array of one to three strings, not a bare string",
        )
    else:
        report.error("required-field", manifest_path, "plugin interface requires a default prompt")
    for url_key in ("websiteURL", "privacyPolicyURL", "termsOfServiceURL"):
        _validate_https_url(report, manifest_path, interface.get(url_key), f"interface.{url_key}")
    brand_color = interface.get("brandColor")
    if brand_color is not None and (
        not isinstance(brand_color, str) or HEX_COLOR_RE.fullmatch(brand_color) is None
    ):
        report.error("color", manifest_path, "interface.brandColor must use `#RRGGBB`")
    for asset_key in ("composerIcon", "logo", "logoDark"):
        if asset_key in interface:
            _contract_path(
                report,
                manifest_path,
                root,
                interface[asset_key],
                f"interface.{asset_key}",
                expected_kind="file",
            )
    screenshots = interface.get("screenshots")
    if screenshots is not None:
        if not isinstance(screenshots, list):
            report.error("type", manifest_path, "interface.screenshots must be an array")
        else:
            for index, screenshot in enumerate(screenshots):
                _contract_path(
                    report,
                    manifest_path,
                    root,
                    screenshot,
                    f"interface.screenshots[{index}]",
                    expected_kind="file",
                )
    return report


def _marketplace_base(path: Path) -> Path:
    # Codex resolves local plugin paths relative to the marketplace root: the
    # directory that contains the recognized manifest layout. Mirror that for each
    # recognized filename so `./`-relative sources resolve the same way it does.
    if path.parent.name == "plugins" and path.parent.parent.name == ".agents":
        return path.parent.parent.parent
    if path.parent.name == ".claude-plugin":
        return path.parent.parent
    return path.parent


def _require_source_string(
    report: ArtifactReport, path: Path, value: Any, label: str
) -> None:
    if not isinstance(value, str) or not value.strip():
        report.error("required-field", path, f"{label} must be a non-empty string")


def _optional_source_string(
    report: ArtifactReport, path: Path, value: Any, label: str
) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        report.error("type", path, f"{label} must be a non-empty string")


def _marketplace_local_root(
    report: ArtifactReport,
    path: Path,
    base: Path,
    raw_path: Any,
    plugin_name: str | None,
    label: str,
) -> None:
    plugin_root = _contract_path(
        report,
        path,
        base,
        raw_path,
        label,
        expected_kind="directory",
        require_exists=False,
    )
    if plugin_root is None:
        return
    if not plugin_root.exists():
        report.error(
            "missing-local-plugin",
            plugin_root,
            "local marketplace plugin source does not exist",
        )
        return
    plugin_manifest = plugin_root / ".codex-plugin" / "plugin.json"
    if plugin_manifest.is_file():
        manifest = _load_json(report, plugin_manifest)
        if manifest is not None and manifest.get("name") != plugin_name:
            report.error(
                "name-mismatch",
                plugin_manifest,
                "marketplace entry name must match plugin.json name",
            )
    else:
        report.error("missing-reference", plugin_root, "local plugin lacks plugin.json")
    plugin_report = _validate_plugin_root(plugin_root, budget=report.budget)
    report.errors.extend(plugin_report.errors)
    report.warnings.extend(plugin_report.warnings)


def _validate_marketplace_file(path: Path, *, budget: ScanBudget | None = None) -> ArtifactReport:
    report = ArtifactReport("marketplace", path, budget=budget or ScanBudget())
    if not _preflight_path(report, path, directory=False):
        return report
    payload = _load_json(report, path)
    if payload is None:
        return report
    _unknown_keys(report, path, payload, {"name", "interface", "plugins"}, "marketplace")
    name = _require_string(report, path, payload, "name", "marketplace field")
    if name is not None:
        _validate_name(report, path, name, "marketplace name")
    interface = payload.get("interface")
    if interface is not None:
        if not isinstance(interface, dict):
            report.error("type", path, "marketplace interface must be an object")
        else:
            _unknown_keys(report, path, interface, {"displayName"}, "marketplace interface")
            if "displayName" in interface and (
                not isinstance(interface["displayName"], str)
                or not interface["displayName"].strip()
            ):
                report.error("type", path, "marketplace interface.displayName must be non-empty")
    plugins = payload.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        report.error("required-field", path, "marketplace requires a non-empty `plugins` array")
        return report
    names: set[str] = set()
    base = _marketplace_base(path)
    for index, entry in enumerate(plugins):
        label = f"plugins[{index}]"
        if not isinstance(entry, dict):
            report.error("plugin-entry", path, f"marketplace {label} must be an object")
            continue
        _unknown_keys(report, path, entry, {"name", "source", "policy", "category"}, label)
        plugin_name = entry.get("name")
        if _validate_name(report, path, plugin_name, f"{label}.name"):
            if plugin_name in names:
                report.error(
                    "duplicate-plugin", path, f"duplicate marketplace plugin `{plugin_name}`"
                )
            names.add(plugin_name)
        source = entry.get("source")
        if isinstance(source, str):
            # Bare-string shorthand is a local path.
            _marketplace_local_root(report, path, base, source, plugin_name, f"{label}.source")
        elif not isinstance(source, dict):
            report.error(
                "required-field",
                path,
                f"{label}.source must be an object or a local-path string",
            )
        else:
            source_type = source.get("source")
            if source_type == "local":
                _unknown_keys(report, path, source, {"source", "path"}, f"{label}.source")
                _marketplace_local_root(
                    report, path, base, source.get("path"), plugin_name, f"{label}.source.path"
                )
            elif source_type == "url":
                _unknown_keys(
                    report,
                    path,
                    source,
                    {"source", "url", "path", "ref", "sha"},
                    f"{label}.source",
                )
                _require_source_string(report, path, source.get("url"), f"{label}.source.url")
                for optional in ("path", "ref", "sha"):
                    _optional_source_string(
                        report, path, source.get(optional), f"{label}.source.{optional}"
                    )
            elif source_type == "git-subdir":
                _unknown_keys(
                    report,
                    path,
                    source,
                    {"source", "url", "path", "ref", "sha"},
                    f"{label}.source",
                )
                _require_source_string(report, path, source.get("url"), f"{label}.source.url")
                _require_source_string(report, path, source.get("path"), f"{label}.source.path")
                for optional in ("ref", "sha"):
                    _optional_source_string(
                        report, path, source.get(optional), f"{label}.source.{optional}"
                    )
            elif source_type == "npm":
                _unknown_keys(
                    report,
                    path,
                    source,
                    {"source", "package", "version", "registry"},
                    f"{label}.source",
                )
                _require_source_string(
                    report, path, source.get("package"), f"{label}.source.package"
                )
                _optional_source_string(
                    report, path, source.get("version"), f"{label}.source.version"
                )
                registry = source.get("registry")
                if registry is not None and (
                    not isinstance(registry, str) or not registry.startswith("https://")
                ):
                    report.error(
                        "source", path, f"{label}.source.registry must be an https:// URL"
                    )
            else:
                report.error(
                    "source",
                    path,
                    f"{label}.source.source must be one of local, url, git-subdir, npm",
                )
        policy = entry.get("policy")
        if not isinstance(policy, dict):
            report.error("required-field", path, f"{label}.policy must be an object")
        else:
            _unknown_keys(
                report,
                path,
                policy,
                {"installation", "authentication", "products"},
                f"{label}.policy",
            )
            if policy.get("installation") not in {
                "NOT_AVAILABLE",
                "AVAILABLE",
                "INSTALLED_BY_DEFAULT",
            }:
                report.error("policy", path, f"{label}.policy.installation is invalid")
            if policy.get("authentication") not in {"ON_INSTALL", "ON_USE"}:
                report.error("policy", path, f"{label}.policy.authentication is invalid")
            if "products" in policy:
                _validate_string_list(
                    report,
                    path,
                    policy["products"],
                    f"{label}.policy.products",
                    allow_empty=False,
                )
        category = entry.get("category")
        if not isinstance(category, str) or not category.strip():
            report.error("required-field", path, f"{label}.category must be a non-empty string")
    return report


def _flatten_keys(value: Any, prefix: str = "") -> set[str]:
    result: set[str] = set()
    if not isinstance(value, dict):
        return result
    for key, child in value.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        result.add(dotted)
        result.update(_flatten_keys(child, dotted))
    return result


def _fallback_toml_keys(text: str) -> set[str]:
    result: set[str] = set()
    table = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        table_match = re.fullmatch(r"\[([^\[\]]+)\]", stripped)
        if table_match:
            table = table_match.group(1).strip()
            result.add(table)
            continue
        key_match = re.match(r"([A-Za-z0-9_-]+)\s*=", stripped)
        if key_match:
            key = key_match.group(1)
            result.add(f"{table}.{key}" if table else key)
    return result


def _validate_approval_policy(report: ArtifactReport, path: Path, value: Any) -> None:
    if isinstance(value, str):
        if value not in {"untrusted", "on-request", "never"}:
            report.error(
                "approval-policy",
                path,
                "approval_policy must be `untrusted`, `on-request`, `never`, or a granular table",
            )
        return
    if not isinstance(value, dict) or set(value) != {"granular"}:
        report.error(
            "approval-policy",
            path,
            "approval_policy table must contain only a `granular` table",
        )
        return
    granular = value.get("granular")
    allowed = {
        "sandbox_approval",
        "rules",
        "mcp_elicitations",
        "request_permissions",
        "skill_approval",
    }
    if not isinstance(granular, dict):
        report.error("approval-policy", path, "approval_policy.granular must be a table")
        return
    _unknown_keys(report, path, granular, allowed, "approval_policy.granular")
    for key, enabled in granular.items():
        if key in allowed and not isinstance(enabled, bool):
            report.error(
                "approval-policy",
                path,
                f"approval_policy.granular.{key} must be a boolean",
            )


def _config_key_checks(
    report: ArtifactReport,
    path: Path,
    payload: dict[str, Any] | None,
    text: str,
    *,
    agent: bool = False,
) -> set[str]:
    keys = _flatten_keys(payload) if payload is not None else _fallback_toml_keys(text)
    leaf_names = {key.rsplit(".", 1)[-1] for key in keys}
    for key in sorted(DEPRECATED_CONFIG_KEYS & leaf_names):
        report.error(
            "deprecated-config", path, f"deprecated Codex config key `{key}` is not allowed"
        )
    for dotted in sorted(LEGACY_WEB_FEATURE_KEYS & keys):
        report.error(
            "deprecated-config", path, f"legacy Codex config key `{dotted}` is not allowed"
        )
    if "profile" in keys or "profiles" in keys or any(key.startswith("profiles.") for key in keys):
        report.error(
            "deprecated-config", path, "legacy profile selectors and `[profiles.*]` are not allowed"
        )
    has_sandbox = "sandbox_mode" in leaf_names or "sandbox_workspace_write" in leaf_names
    has_permissions = "default_permissions" in leaf_names or any(
        key == "permissions" or key.startswith("permissions.") for key in keys
    )
    if has_sandbox and has_permissions:
        report.error(
            "mixed-permissions",
            path,
            "permission profiles (`default_permissions` or `[permissions]`) must not be mixed with `sandbox_mode`",
        )
    if payload is not None:
        if "approval_policy" in payload:
            _validate_approval_policy(report, path, payload["approval_policy"])
        default_permissions = payload.get("default_permissions")
        if default_permissions is not None:
            if not isinstance(default_permissions, str) or not default_permissions.strip():
                report.error(
                    "permission-profile",
                    path,
                    "default_permissions must be a non-empty profile name",
                )
            elif default_permissions.startswith(":") and default_permissions not in {
                ":read-only",
                ":workspace",
                ":danger-full-access",
            }:
                report.error(
                    "permission-profile",
                    path,
                    "default_permissions uses an unknown built-in profile",
                )
        sandbox_mode = payload.get("sandbox_mode")
        if sandbox_mode is not None and sandbox_mode not in {
            "read-only",
            "workspace-write",
            "danger-full-access",
        }:
            report.error("sandbox-mode", path, "sandbox_mode is not a supported Codex value")
        features = payload.get("features")
        if isinstance(features, dict):
            for feature in ("hooks", "multi_agent"):
                if feature in features and not isinstance(features[feature], bool):
                    report.error("type", path, f"features.{feature} must be a boolean")
        effort = payload.get("model_reasoning_effort")
        allowed_efforts = AGENT_REASONING_EFFORTS if agent else CONFIG_REASONING_EFFORTS
        if effort is not None and effort not in allowed_efforts:
            report.error(
                "reasoning-effort", path, "model_reasoning_effort is not a supported value"
            )
        plan_effort = payload.get("plan_mode_reasoning_effort")
        if plan_effort is not None and plan_effort not in PLAN_REASONING_EFFORTS:
            report.error(
                "reasoning-effort",
                path,
                "plan_mode_reasoning_effort is not a supported value",
            )
        if "mcp_servers" in payload:
            _validate_mcp_servers(report, path, payload["mcp_servers"], path.parent)
    return keys


def _validate_agent_file(path: Path, *, budget: ScanBudget | None = None) -> ArtifactReport:
    report = ArtifactReport("agent", path, budget=budget or ScanBudget())
    if not _preflight_path(report, path, directory=False):
        return report
    payload, text = _load_toml(report, path)
    if text is None:
        return report
    _config_key_checks(report, path, payload, text, agent=True)
    if payload is None:
        for key in ("name", "description", "developer_instructions"):
            if re.search(rf"(?m)^\s*{re.escape(key)}\s*=", text) is None:
                report.error("required-field", path, f"agent requires `{key}`")
        return report
    _unknown_keys(report, path, payload, CONFIG_TOP_LEVEL_KEYS | AGENT_FIELDS, "agent")
    name = _require_string(report, path, payload, "name", "agent field")
    if name is not None and IDENTIFIER_RE.fullmatch(name) is None:
        report.error(
            "name",
            path,
            "agent name must use ASCII letters, digits, hyphens, or underscores",
        )
    _require_string(report, path, payload, "description", "agent field")
    _require_string(report, path, payload, "developer_instructions", "agent field")
    nicknames = payload.get("nickname_candidates")
    if nicknames is not None:
        valid_nicknames = (
            isinstance(nicknames, list)
            and bool(nicknames)
            and all(
                isinstance(nickname, str) and re.fullmatch(r"[A-Za-z0-9 _-]+", nickname) is not None
                for nickname in nicknames
            )
            and len(set(nicknames)) == len(nicknames)
        )
        if not valid_nicknames:
            report.error(
                "nickname-candidates",
                path,
                "agent nickname_candidates must be a non-empty list of unique printable ASCII names",
            )
    return report


def _validate_config_file(path: Path, *, budget: ScanBudget | None = None) -> ArtifactReport:
    report = ArtifactReport("config", path, budget=budget or ScanBudget())
    if not _preflight_path(report, path, directory=False):
        return report
    payload, text = _load_toml(report, path)
    if text is not None:
        _config_key_checks(report, path, payload, text)
    if payload is not None:
        _unknown_keys(
            report,
            path,
            payload,
            CONFIG_TOP_LEVEL_KEYS,
            "Codex 0.145.0 config top-level",
        )
    return report


REQUIREMENTS_TOP_LEVEL_KEYS = {
    "allowed_approval_policies",
    "allowed_approvals_reviewers",
    "allowed_sandbox_modes",
    "allowed_permission_profiles",
    "default_permissions",
    "remote_sandbox_config",
    "allowed_web_search_modes",
    "allow_managed_hooks_only",
    "allow_appshots",
    "allow_remote_control",
    "computer_use",
    "windows",
    # `features` is the primary key; `feature_requirements` is its serde alias.
    "features",
    "feature_requirements",
    "hooks",
    "mcp_servers",
    "plugins",
    "marketplaces",
    "apps",
    "rules",
    "enforce_residency",
    # The struct field is `network` but its serde rename is `experimental_network`.
    "experimental_network",
    "permissions",
    "models",
    "guardian_policy_config",
}
_BUILTIN_PERMISSION_PROFILES = frozenset({":read-only", ":workspace", ":danger-full-access"})


def _validate_requirements_file(
    path: Path, *, budget: ScanBudget | None = None
) -> ArtifactReport:
    report = ArtifactReport("requirements", path, budget=budget or ScanBudget())
    if not _preflight_path(report, path, directory=False):
        return report
    payload, _text = _load_toml(report, path)
    if payload is None:
        # _load_toml already emitted a fail-closed error (unreadable, Python 3.10
        # without tomllib, invalid TOML, or a non-table root).
        return report
    # Do NOT reuse _config_key_checks here: requirements.toml has its own key surface
    # (REQUIREMENTS_TOP_LEVEL_KEYS), and its `mcp_servers`/`features` values use managed
    # requirement shapes (McpServerRequirement, a feature->bool map) distinct from
    # config.toml, so config-shape validation would reject valid managed files.
    _unknown_keys(
        report,
        path,
        payload,
        REQUIREMENTS_TOP_LEVEL_KEYS,
        "Codex 0.145.0 requirements top-level",
    )
    allowed = payload.get("allowed_permission_profiles")
    default_permissions = payload.get("default_permissions")
    if allowed is None:
        # Codex requires allowed_permission_profiles whenever default_permissions is set.
        if default_permissions is not None:
            report.error(
                "permission-profile",
                path,
                "default_permissions requires allowed_permission_profiles",
            )
        _requirements_flag_checks(report, path, payload)
        return report
    if not isinstance(allowed, dict) or not allowed:
        report.error(
            "permission-profile",
            path,
            "allowed_permission_profiles must be a non-empty table of profile name to boolean",
        )
        _requirements_flag_checks(report, path, payload)
        return report
    # A profile counts as allowed only when present AND mapped to true (Codex
    # `is_permission_allowed`). Non-built-in profiles may be defined in a lower
    # config layer, so a single-file static check must not reject them.
    allowed_true: set[str] = set()
    for profile, enabled in allowed.items():
        if not isinstance(profile, str) or not profile.strip():
            report.error(
                "permission-profile",
                path,
                "allowed_permission_profiles keys must be non-empty profile names",
            )
            continue
        if not isinstance(enabled, bool):
            report.error(
                "permission-profile",
                path,
                f"allowed_permission_profiles entry `{profile}` must map to a boolean",
            )
            continue
        if profile.startswith(":") and profile not in _BUILTIN_PERMISSION_PROFILES:
            report.error(
                "permission-profile",
                path,
                f"allowed_permission_profiles entry `{profile}` is an unknown built-in profile",
            )
        if enabled:
            allowed_true.add(profile)
    # Resolve the effective default as Codex does: the explicit default, else the
    # implicit `:workspace` only when both `:workspace` and `:read-only` are allowed.
    if isinstance(default_permissions, str) and default_permissions.strip():
        effective_default: str | None = default_permissions
    elif default_permissions is None and {":workspace", ":read-only"} <= allowed_true:
        effective_default = ":workspace"
    else:
        effective_default = None
    if effective_default is None:
        report.error(
            "permission-profile",
            path,
            "default_permissions must be set unless allowed_permission_profiles allows both :read-only and :workspace",
        )
    elif effective_default not in allowed_true:
        report.error(
            "permission-profile",
            path,
            f"default_permissions `{effective_default}` must be allowed (set to true) by allowed_permission_profiles",
        )
    _requirements_flag_checks(report, path, payload)
    return report


def _requirements_flag_checks(report: ArtifactReport, path: Path, payload: dict[str, Any]) -> None:
    for bool_key in ("allow_managed_hooks_only", "allow_appshots", "allow_remote_control"):
        value = payload.get(bool_key)
        if value is not None and not isinstance(value, bool):
            report.error("type", path, f"{bool_key} must be a boolean")
    guardian = payload.get("guardian_policy_config")
    if guardian is not None and (not isinstance(guardian, str) or not guardian.strip()):
        report.error("type", path, "guardian_policy_config must be a non-empty string")


def _validate_instructions_file(path: Path, *, budget: ScanBudget | None = None) -> ArtifactReport:
    report = ArtifactReport("instructions", path, budget=budget or ScanBudget())
    if not _preflight_path(report, path, directory=False):
        return report
    text = _read_text(report, path, limit=MAX_INSTRUCTIONS_BYTES)
    if text is not None and not text.strip():
        report.error("empty", path, "instruction file must not be empty")
    return report


def _literal_string_list(node: ast.AST, *, allow_empty: bool = False) -> list[str] | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or not all(isinstance(item, str) and item for item in value)
    ):
        return None
    return value


def _literal_rule_pattern(node: ast.AST) -> list[str | list[str]] | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    if not isinstance(value, list) or not value:
        return None
    result: list[str | list[str]] = []
    for element in value:
        if isinstance(element, str) and element:
            result.append(element)
            continue
        if (
            isinstance(element, list)
            and bool(element)
            and all(isinstance(option, str) and option for option in element)
            and len(set(element)) == len(element)
        ):
            result.append(element)
            continue
        return None
    return result


def _rule_pattern_matches(tokens: list[str], pattern: list[str | list[str]]) -> bool:
    if len(tokens) < len(pattern):
        return False
    for token, expected in zip(tokens, pattern):
        if isinstance(expected, str):
            if token != expected:
                return False
        elif token not in expected:
            return False
    return True


def _validate_rule_file(path: Path, *, budget: ScanBudget | None = None) -> ArtifactReport:
    report = ArtifactReport("rule", path, budget=budget or ScanBudget())
    if not _preflight_path(report, path, directory=False):
        return report
    text = _read_text(report, path)
    if text is None:
        return report
    try:
        module = ast.parse(text, filename=str(path), mode="exec")
    except SyntaxError as exc:
        report.error("rule-syntax", path, f"invalid rule syntax at line {exc.lineno}: {exc.msg}")
        return report
    if not module.body:
        report.error("empty", path, "rule file requires at least one prefix_rule")
        return report
    rule_count = 0
    for index, statement in enumerate(module.body):
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == "prefix_rule"
        ):
            report.error("rule-shape", path, f"statement {index + 1} must be a prefix_rule call")
            continue
        call = statement.value
        rule_count += 1
        if call.args:
            report.error("rule-shape", path, f"prefix_rule {rule_count} must use keyword arguments")
        keywords: dict[str, ast.AST] = {}
        for keyword in call.keywords:
            if keyword.arg is None:
                report.error("rule-shape", path, f"prefix_rule {rule_count} must not use **kwargs")
                continue
            if keyword.arg in keywords:
                report.error(
                    "rule-shape", path, f"prefix_rule {rule_count} duplicates `{keyword.arg}`"
                )
            keywords[keyword.arg] = keyword.value
        for key in sorted(
            set(keywords) - {"pattern", "decision", "justification", "match", "not_match"}
        ):
            report.error("unknown-field", path, f"prefix_rule {rule_count} has unsupported `{key}`")
        pattern = _literal_rule_pattern(keywords["pattern"]) if "pattern" in keywords else None
        if pattern is None:
            report.error(
                "rule-pattern",
                path,
                f"prefix_rule {rule_count} pattern elements must be strings or non-empty unique string unions",
            )
        try:
            decision = ast.literal_eval(keywords["decision"]) if "decision" in keywords else "allow"
        except (ValueError, TypeError, SyntaxError):
            decision = None
        if decision not in {"allow", "prompt", "forbidden"}:
            report.error(
                "rule-decision",
                path,
                f"prefix_rule {rule_count} decision must be `allow`, `prompt`, or `forbidden`",
            )
        match_cases = (
            _literal_string_list(keywords["match"], allow_empty=True)
            if "match" in keywords
            else None
        )
        not_match_cases = (
            _literal_string_list(keywords["not_match"], allow_empty=True)
            if "not_match" in keywords
            else None
        )
        if "match" not in keywords and "not_match" not in keywords:
            report.warning(
                "rule-cases-unproven",
                path,
                f"prefix_rule {rule_count} has no inline positive or negative examples",
            )
        if "match" in keywords and match_cases is None:
            report.error(
                "rule-cases", path, f"prefix_rule {rule_count} match must be a string list"
            )
        if "not_match" in keywords and not_match_cases is None:
            report.error(
                "rule-cases", path, f"prefix_rule {rule_count} not_match must be a string list"
            )
        if "justification" in keywords:
            try:
                justification = ast.literal_eval(keywords["justification"])
            except (ValueError, TypeError, SyntaxError):
                justification = None
            if not isinstance(justification, str) or not justification.strip():
                report.error(
                    "rule-justification",
                    path,
                    f"prefix_rule {rule_count} justification must be a non-empty string",
                )
        if pattern is not None:
            for case in match_cases or []:
                try:
                    tokens = shlex.split(case)
                except ValueError:
                    report.error(
                        "rule-cases", path, f"prefix_rule {rule_count} has invalid match case"
                    )
                    continue
                if not _rule_pattern_matches(tokens, pattern):
                    report.error(
                        "rule-cases",
                        path,
                        f"prefix_rule {rule_count} match case `{case}` does not start with its pattern",
                    )
            for case in not_match_cases or []:
                try:
                    tokens = shlex.split(case)
                except ValueError:
                    report.error(
                        "rule-cases", path, f"prefix_rule {rule_count} has invalid not_match case"
                    )
                    continue
                if _rule_pattern_matches(tokens, pattern):
                    report.error(
                        "rule-cases",
                        path,
                        f"prefix_rule {rule_count} not_match case `{case}` unexpectedly matches its pattern",
                    )
    if rule_count == 0:
        report.error("rule-shape", path, "rule file requires at least one prefix_rule")
    return report


def _resolve_specific_path(kind: str, raw_path: Path) -> Path:
    path = _absolute_path(raw_path)
    if _is_symlink(path):
        return path
    if kind == "skill" and path.name == "SKILL.md":
        return path.parent
    if kind == "plugin" and path.name == "plugin.json" and path.parent.name == ".codex-plugin":
        return path.parent.parent
    filename_by_kind = {
        "marketplace": "marketplace.json",
        "hook": "hooks.json",
        "mcp": ".mcp.json",
        "app": ".app.json",
        "config": "config.toml",
        "instructions": "AGENTS.md",
        "requirements": "requirements.toml",
    }
    filename = filename_by_kind.get(kind)
    if filename is not None and path.is_dir():
        if kind == "instructions" and not (path / filename).exists():
            override = path / "AGENTS.override.md"
            if override.exists():
                return override
        return path / filename
    return path


VALIDATORS: dict[str, Callable[..., ArtifactReport]] = {
    "skill": _validate_skill_root,
    "plugin": _validate_plugin_root,
    "marketplace": _validate_marketplace_file,
    "agent": _validate_agent_file,
    "hook": _validate_hook_file,
    "mcp": _validate_mcp_file,
    "app": _validate_app_file,
    "config": _validate_config_file,
    "instructions": _validate_instructions_file,
    "rule": _validate_rule_file,
    "requirements": _validate_requirements_file,
}


def _artifact_candidate(path: Path) -> tuple[str, Path] | None:
    if path.name == "SKILL.md":
        return "skill", path.parent
    if path.name == "plugin.json" and path.parent.name == ".codex-plugin":
        return "plugin", path.parent.parent
    if (
        path.name in {"marketplace.json", "api_marketplace.json"}
        and path.parent.name == "plugins"
        and path.parent.parent.name == ".agents"
    ):
        return "marketplace", path
    if path.name == "marketplace.json" and path.parent.name == ".claude-plugin":
        return "marketplace", path
    if path.name == "hooks.json":
        return "hook", path
    if path.name == ".mcp.json":
        return "mcp", path
    if path.name == ".app.json":
        return "app", path
    if path.name in {"AGENTS.md", "AGENTS.override.md"}:
        return "instructions", path
    if path.suffix == ".rules":
        return "rule", path
    if path.name == "requirements.toml":
        return "requirements", path
    if path.name == "config.toml" or path.name.endswith(".config.toml"):
        return "config", path
    if path.suffix == ".toml" and "agents" in path.parts:
        return "agent", path
    return None


def _discover(
    root: Path,
    report: ArtifactReport,
) -> tuple[list[tuple[str, Path]], list[Path]]:
    artifacts: set[tuple[str, Path]] = set()
    symlinks: list[Path] = []
    if not report.budget.consume_entry(report, root):
        return [], []
    for current_raw, directories, files in os.walk(
        root,
        followlinks=False,
        onerror=_walk_onerror(report, root),
    ):
        current = Path(current_raw)
        kept: list[str] = []
        for name in sorted(directories):
            candidate = current / name
            if name in SKIP_DISCOVERY_DIRS:
                continue
            if not report.budget.consume_entry(report, candidate):
                directories[:] = []
                return sorted(artifacts), sorted(symlinks)
            if _is_symlink(candidate):
                symlinks.append(candidate)
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            candidate = current / name
            if not report.budget.consume_entry(report, candidate):
                directories[:] = []
                return sorted(artifacts), sorted(symlinks)
            if _is_symlink(candidate):
                if _artifact_candidate(candidate) is not None:
                    symlinks.append(candidate)
                continue
            artifact = _artifact_candidate(candidate)
            if artifact is not None:
                if artifact not in artifacts and len(artifacts) >= MAX_DISCOVERED_ARTIFACTS:
                    report.error(
                        "artifact-limit",
                        candidate,
                        "artifact discovery exceeds the safety limit of "
                        f"{MAX_DISCOVERED_ARTIFACTS}",
                    )
                    directories[:] = []
                    return sorted(artifacts), sorted(symlinks)
                artifacts.add(artifact)
    return sorted(artifacts, key=lambda item: (item[0], str(item[1]))), sorted(symlinks)


def _validate_all(root: Path, *, budget: ScanBudget | None = None) -> list[ArtifactReport]:
    discovery = ArtifactReport("all", root, budget=budget or ScanBudget())
    if not _preflight_path(discovery, root, directory=True):
        return [discovery]
    artifacts, symlinks = _discover(root, discovery)
    for path in symlinks:
        discovery.error("symlink", path, "symlink artifacts are not allowed")
    if not artifacts:
        discovery.error("no-artifacts", root, "no supported Codex artifacts were discovered")
        return [discovery]
    reports = [VALIDATORS[kind](path, budget=discovery.budget) for kind, path in artifacts]
    if discovery.errors or discovery.warnings:
        reports.insert(0, discovery)
    return reports


def _payload(kind: str, path: Path, reports: Sequence[ArtifactReport]) -> dict[str, Any]:
    error_count = sum(len(set(report.errors)) for report in reports)
    warning_count = sum(len(set(report.warnings)) for report in reports)
    return {
        "artifacts": [report.to_dict() for report in reports],
        "kind": kind,
        "path": _display_path(path),
        "status": "FAIL" if error_count else "PASS",
        "summary": {
            "checked": len(reports),
            "errors": error_count,
            "warnings": warning_count,
        },
    }


def _print_text(payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    print(
        f"{payload['status']} {payload['kind']} {payload['path']} "
        f"(checked={summary['checked']} errors={summary['errors']} warnings={summary['warnings']})"
    )
    for artifact in payload["artifacts"]:
        for error in artifact["errors"]:
            print(f"ERROR {error['code']} {error['path']}: {error['message']}")
        for warning in artifact["warnings"]:
            print(f"WARN {warning['code']} {warning['path']}: {warning['message']}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a Codex-native artifact without dependencies or side effects."
    )
    parser.add_argument("kind", choices=KINDS)
    parser.add_argument("path")
    parser.add_argument("--json", action="store_true", help="emit one stable JSON result object")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    requested_path = _absolute_path(args.path)
    try:
        if args.kind == "all":
            reports = _validate_all(requested_path)
            effective_path = requested_path
        else:
            effective_path = _resolve_specific_path(args.kind, requested_path)
            reports = [VALIDATORS[args.kind](effective_path)]
        result = _payload(args.kind, effective_path, reports)
    except Exception as exc:  # Keep unexpected failures distinct from validation failures.
        failure = {
            "artifacts": [],
            "kind": args.kind,
            "path": _display_path(requested_path),
            "status": "ERROR",
            "summary": {"checked": 0, "errors": 1, "warnings": 0},
            "error": {"code": "internal-error", "message": str(exc)},
        }
        if args.json:
            print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        else:
            print(f"ERROR internal-error {requested_path}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        _print_text(result)
    return 1 if result["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
