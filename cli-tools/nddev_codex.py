#!/usr/bin/env python3
"""Transactional setup manager for a caller-selected Codex home."""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import hashlib
import json
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = ROOT / "setups"
VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()
PRODUCT_NAME = "nddev-codex-app"
STAMP_NAME = "NDDEV-CODEX-SETUP.json"
BACKUP_NAME = "NDDEV-CODEX-BACKUP.json"
MANAGED_FILES = ("config.toml", "AGENTS.md")
OVERRIDE_NAME = "AGENTS.override.md"
BUILDER_PROFILE_NAME = "nddev-builder.config.toml"
BUILDER_PROFILE_ID = "nddev-builder"
BUILDER_MARKETPLACE_ID = "nddev-builder"
BUILDER_PLUGIN_ID = "nddev-builder"
BUILDER_PLUGIN_QUALIFIED_ID = f"{BUILDER_PLUGIN_ID}@{BUILDER_MARKETPLACE_ID}"
OWNER_FILE_MODE = 0o600
OWNER_DIRECTORY_MODE = 0o700
METADATA_MAX_BYTES = 256 * 1024
MANAGED_PAYLOAD_MAX_BYTES = 8 * 1024 * 1024
TESTED_CODEX_VERSION = "0.145.0"
INSTALLER_RELEASE_TAG = f"rust-v{TESTED_CODEX_VERSION}"
INSTALLER_NAME = "install.sh"
INSTALLER_URL = (
    f"https://github.com/openai/codex/releases/download/{INSTALLER_RELEASE_TAG}/{INSTALLER_NAME}"
)
INSTALLER_SIZE_BYTES = 25_133
INSTALLER_SHA256 = "1154e9daf713aacd1534efca8042bfd6665ad24bc1d1dfd86b8f439fe60a7a5d"
RELEASE_METADATA_URL = (
    f"https://api.github.com/repos/openai/codex/releases/tags/{INSTALLER_RELEASE_TAG}"
)
PACKAGE_CHECKSUM_ASSET = "codex-package_SHA256SUMS"
PACKAGE_CHECKSUM_SHA256 = "db72a7585c594e141201dea9fea37a3686d2668aaee603b96794712c8e394e0d"
PACKAGE_ASSETS = {
    "aarch64-apple-darwin": (
        "codex-package-aarch64-apple-darwin.tar.gz",
        "bcbfa76650b6c581505aa5178c1e799d37ff12fc43a35ff16c90b97fa757e63f",
    ),
    "x86_64-apple-darwin": (
        "codex-package-x86_64-apple-darwin.tar.gz",
        "daa3df37c8a041280f52a2198dbe7acbead64936b23f8b660edf9d886df5f9da",
    ),
    "aarch64-unknown-linux-musl": (
        "codex-package-aarch64-unknown-linux-musl.tar.gz",
        "b4359896bb548e02fdd72ea0cb3395fe8a88d20d3a4a421c4481e504f8e8927f",
    ),
    "x86_64-unknown-linux-musl": (
        "codex-package-x86_64-unknown-linux-musl.tar.gz",
        "99ae48e4743da6c530ecd998ab2f7e66572c092f4190c88dca8236c07b06ce1d",
    ),
}
INSTALLER_MAX_BYTES = 64 * 1024
PACKAGE_METADATA_MAX_BYTES = 64 * 1024
VERSION_OUTPUT_MAX_BYTES = 4 * 1024
INSTALLER_TIMEOUT_SECONDS = 600
VERSION_TIMEOUT_SECONDS = 15
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
INSTALLER_OUTPUT_MAX_BYTES = 256 * 1024
BUILDER_COMMAND_OUTPUT_MAX_BYTES = 256 * 1024
BUILDER_COMMAND_TIMEOUT_SECONDS = 120
BUILDER_TREE_MAX_FILES = 256
BUILDER_TREE_MAX_DIRECTORIES = 256
BUILDER_TREE_MAX_BYTES = 8 * 1024 * 1024
INSTALLER_TERMINATION_GRACE_SECONDS = 0.5
INSTALLER_KILL_WAIT_SECONDS = 2
CONTROLLED_INSTALLER_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
PACKAGE_METADATA_KEYS = {
    "layoutVersion",
    "version",
    "target",
    "variant",
    "entrypoint",
    "resourcesDir",
    "pathDir",
}
SETUP_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
SEMVER_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
PERMISSION_CONFIG_KEYS = ("default_permissions", "approval_policy")
PERMISSION_CONFIG_LINE_PATTERN = re.compile(
    r'(?P<key>[A-Za-z_][A-Za-z0-9_]*) = "(?P<value>[^"\\\r\n]*)"\Z'
)
STAMP_KEYS = {
    "schema_version",
    "product_name",
    "build_version",
    "setup_id",
    "canonical_target",
    "managed_files",
}
BACKUP_KEYS = {
    "schema_version",
    "product_name",
    "build_version",
    "slot",
    "canonical_target",
    "source_setup_id",
    "managed_files",
    "stamp_sha256",
}


class CodexSetupError(Exception):
    """A safe, user-facing lifecycle failure."""


class ConcurrentTargetChange(CodexSetupError):
    """A fail-closed target identity or managed-entry race."""


PathIdentity = tuple[int, int]


@dataclass(frozen=True)
class FileSnapshot:
    identity: PathIdentity
    digest: str
    mode: int
    owner: int | None


@dataclass(frozen=True)
class BuilderCacheDirectorySnapshot:
    identity: PathIdentity
    mode: int


@dataclass(frozen=True)
class BuilderCacheTreeSnapshot:
    root_identity: PathIdentity = field(compare=False)
    root_mode: int
    directory_modes: dict[str, int]
    files: dict[str, tuple[bytes, int]]


@dataclass(frozen=True)
class BuilderCacheTransactionSnapshot:
    ancestors: tuple[BuilderCacheDirectorySnapshot | None, ...]
    tree: BuilderCacheTreeSnapshot | None


@dataclass
class TargetGuard:
    target: Path
    parent_identity: PathIdentity
    target_identity: PathIdentity | None
    parent_fd: int | None = None
    target_fd: int | None = None
    created_target: bool = False
    expected_managed: dict[str, FileSnapshot | None] | None = None
    mutated_paths: set[str] = field(default_factory=set)
    manager_results: dict[str, FileSnapshot | None] = field(default_factory=dict)


@dataclass(frozen=True)
class BackupSlotChoice:
    slot: int
    expected_identity: PathIdentity | None


@dataclass
class BackupPoolLease:
    target: Path
    pool: Path
    fd: int
    identity: PathIdentity
    closed: bool = False


@dataclass(frozen=True)
class SoftwareInstallation:
    version: str
    executable: Path
    release_directory: Path
    host_target: str


ACTIVE_TARGET_GUARD: contextvars.ContextVar[TargetGuard | None] = contextvars.ContextVar(
    "nddev_codex_target_guard", default=None
)


def fail(message: str) -> NoReturn:
    raise CodexSetupError(message)


def fail_concurrent(message: str) -> NoReturn:
    raise ConcurrentTargetChange(message)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def identity_of(info: os.stat_result) -> PathIdentity:
    return info.st_dev, info.st_ino


def owner_of(info: os.stat_result) -> int | None:
    return info.st_uid if hasattr(info, "st_uid") else None


def is_owner_only_file(info: os.stat_result) -> bool:
    if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE:
        return False
    if hasattr(os, "geteuid") and owner_of(info) != os.geteuid():
        return False
    return True


def require_directory(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail(f"{label} must be a real directory")
    return info


def require_regular_file(path: Path, label: str, *, owner_only: bool = False) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular non-symlink file")
    if info.st_nlink != 1:
        fail(f"{label} must not have hard-link aliases")
    if owner_only and not is_owner_only_file(info):
        fail(f"{label} must be owned by the current user with mode 0600")
    return info


def require_bounded_size(
    info: os.stat_result,
    label: str,
    max_bytes: int,
) -> None:
    if info.st_size > max_bytes:
        fail(f"{label} exceeds the {max_bytes}-byte size limit")


def read_regular_file(
    path: Path,
    label: str,
    *,
    owner_only: bool = False,
    max_bytes: int = MANAGED_PAYLOAD_MAX_BYTES,
) -> tuple[bytes, os.stat_result]:
    before = require_regular_file(path, label, owner_only=owner_only)
    require_bounded_size(before, label, max_bytes)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if identity_of(opened) != identity_of(before):
            fail(f"{label} changed while it was being opened")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            fail(f"{label} changed to an unsafe file")
        require_bounded_size(opened, label, max_bytes)
        if owner_only and not is_owner_only_file(opened):
            fail(f"{label} must be owned by the current user with mode 0600")
        blocks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, 65536)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                fail(f"{label} exceeds the {max_bytes}-byte size limit")
            blocks.append(block)
        after = os.fstat(descriptor)
        require_bounded_size(after, label, max_bytes)
    finally:
        os.close(descriptor)
    final = require_regular_file(path, label, owner_only=owner_only)
    require_bounded_size(final, label, max_bytes)
    expected = identity_of(before)
    if identity_of(after) != expected or identity_of(final) != expected:
        fail(f"{label} changed while it was being read")
    return b"".join(blocks), final


def require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        fail(f"{label} has invalid keys (missing={missing}, extra={extra})")


def parse_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read valid JSON from {label}: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must contain a JSON object")
    return value


def load_json_object(path: Path, label: str, *, owner_only: bool = False) -> dict[str, Any]:
    content, _ = read_regular_file(
        path,
        label,
        owner_only=owner_only,
        max_bytes=METADATA_MAX_BYTES,
    )
    return parse_json_object(content, label)


def validate_setup_id(setup_id: str) -> None:
    if not SETUP_ID_PATTERN.fullmatch(setup_id):
        fail(f"invalid setup id: {setup_id!r}")


def parse_permission_config(content: str, label: str) -> dict[str, str]:
    if not content.endswith("\n") or "\r" in content:
        fail(f"{label} must be LF-terminated text")
    lines = content.splitlines()
    if len(lines) != len(PERMISSION_CONFIG_KEYS) or any(not line for line in lines):
        fail(f"{label} must contain exactly two non-empty lines")

    config: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        match = PERMISSION_CONFIG_LINE_PATTERN.fullmatch(line)
        if match is None:
            fail(f"{label} has malformed line {line_number}")
        key = match.group("key")
        if key not in PERMISSION_CONFIG_KEYS:
            fail(f"{label} has unknown key {key!r}")
        if key in config:
            fail(f"{label} has duplicate key {key!r}")
        config[key] = match.group("value")

    if tuple(config) != PERMISSION_CONFIG_KEYS:
        fail(f"{label} keys must appear in the canonical order")
    return config


def render_setup(setup_id: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    validate_setup_id(setup_id)
    setup_root = CATALOG_ROOT / setup_id
    if not setup_root.is_dir() or setup_root.is_symlink():
        fail(f"unknown setup: {setup_id}")

    metadata = load_json_object(setup_root / "setup.json", f"setup {setup_id} metadata")
    require_exact_keys(
        metadata,
        {"schema_version", "id", "description", "managed_files"},
        f"setup {setup_id} metadata",
    )
    if metadata["schema_version"] != 1:
        fail(f"setup {setup_id} metadata has unsupported schema")
    if metadata["id"] != setup_id:
        fail(f"setup {setup_id} metadata identity mismatch")
    if not isinstance(metadata["description"], str) or not metadata["description"].strip():
        fail(f"setup {setup_id} description must be non-empty")
    if metadata["managed_files"] != list(MANAGED_FILES):
        fail(f"setup {setup_id} managed file declaration is invalid")

    rendered: dict[str, bytes] = {}
    for name in MANAGED_FILES:
        path = setup_root / name
        try:
            content, _ = read_regular_file(path, f"setup {setup_id}/{name}")
            content.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            fail(f"setup {setup_id}/{name} must be valid UTF-8: {exc}")
        if not content or not content.endswith(b"\n") or b"\r" in content:
            fail(f"setup {setup_id}/{name} must be non-empty LF-terminated text")
        rendered[name] = content

    config = parse_permission_config(
        rendered["config.toml"].decode("utf-8"),
        f"setup {setup_id}/config.toml",
    )
    expected_permissions = {
        "safe": (":read-only", "on-request"),
        "full-auto": (":danger-full-access", "never"),
    }
    if setup_id not in expected_permissions:
        fail(f"unsupported setup id: {setup_id}")
    actual_permissions = (config["default_permissions"], config["approval_policy"])
    if actual_permissions != expected_permissions[setup_id]:
        fail(f"setup {setup_id}/config.toml permission contract mismatch")
    return metadata, rendered


def list_setups() -> list[dict[str, Any]]:
    if not CATALOG_ROOT.is_dir() or CATALOG_ROOT.is_symlink():
        fail("setup catalog is missing or unsafe")
    entries: list[dict[str, Any]] = []
    for candidate in sorted(CATALOG_ROOT.iterdir(), key=lambda path: path.name):
        if not candidate.is_dir() or candidate.is_symlink():
            fail(f"catalog entry must be a real directory: {candidate.name}")
        metadata, _ = render_setup(candidate.name)
        entries.append(
            {
                "id": metadata["id"],
                "description": metadata["description"],
                "managed_files": metadata["managed_files"],
            }
        )
    if not entries:
        fail("setup catalog is empty")
    return entries


def resolve_target(raw_target: str) -> Path:
    expanded = Path(raw_target).expanduser()
    if not expanded.is_absolute():
        fail("--target must be an absolute path")
    try:
        raw_info = expanded.lstat()
    except FileNotFoundError:
        raw_info = None
    if raw_info is not None and stat.S_ISLNK(raw_info.st_mode):
        fail("--target must not be a symlink")
    target = expanded.resolve(strict=False)
    if target == Path(target.anchor):
        fail("filesystem root cannot be a target")
    parent = target.parent
    try:
        parent_info = parent.lstat()
    except FileNotFoundError:
        fail("--target parent must already exist")
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        fail("canonical --target parent must be a real directory")
    if target.exists():
        target_info = target.lstat()
        if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISDIR(target_info.st_mode):
            fail("--target must be a real directory when it exists")
    return target


def anchored_directory_operations_supported() -> bool:
    required = (os.open, os.stat, os.mkdir, os.unlink, os.rmdir, os.rename, os.link)
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "fchmod")
        and os.listdir in os.supports_fd
        and all(operation in os.supports_dir_fd for operation in required)
    )


def directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def open_directory_fd(path: Path | str, *, dir_fd: int | None = None) -> int:
    if dir_fd is None:
        descriptor = os.open(path, directory_open_flags())
    else:
        descriptor = os.open(path, directory_open_flags(), dir_fd=dir_fd)
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        fail(f"directory handle is unsafe: {path}")
    return descriptor


def anchored_rename(
    source: str,
    destination: str,
    *,
    source_fd: int,
    destination_fd: int,
) -> None:
    """Rename one anchored entry without resolving either directory by path."""
    os.rename(
        source,
        destination,
        src_dir_fd=source_fd,
        dst_dir_fd=destination_fd,
    )


def current_target_guard(target: Path) -> TargetGuard | None:
    guard = ACTIVE_TARGET_GUARD.get()
    if guard is None or guard.target != target:
        return None
    return guard


def revalidate_guard_parent(guard: TargetGuard) -> None:
    parent_info = require_directory(guard.target.parent, "canonical --target parent")
    if identity_of(parent_info) != guard.parent_identity:
        fail_concurrent("canonical --target parent changed during the operation")
    if guard.parent_fd is not None:
        if identity_of(os.fstat(guard.parent_fd)) != guard.parent_identity:
            fail_concurrent("canonical --target parent handle changed during the operation")


def revalidate_held_target(guard: TargetGuard) -> None:
    if guard.target_identity is None or guard.target_fd is None:
        fail("operation requires an anchored target directory handle")
    if identity_of(os.fstat(guard.target_fd)) != guard.target_identity:
        fail_concurrent("held --target directory changed during the operation")


def revalidate_guard(guard: TargetGuard, *, allow_missing: bool) -> None:
    revalidate_guard_parent(guard)

    try:
        if guard.parent_fd is not None:
            target_info = os.stat(
                guard.target.name,
                dir_fd=guard.parent_fd,
                follow_symlinks=False,
            )
        else:
            target_info = guard.target.lstat()
    except FileNotFoundError:
        if guard.target_identity is None and allow_missing:
            return
        fail_concurrent("--target changed or disappeared during the operation")

    if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISDIR(target_info.st_mode):
        fail_concurrent("--target changed to an unsafe path during the operation")
    if guard.target_identity is None:
        fail_concurrent("--target appeared concurrently during the operation")
    if identity_of(target_info) != guard.target_identity:
        fail_concurrent("--target identity changed during the operation")
    if guard.target_fd is not None:
        revalidate_held_target(guard)


def open_created_target_directory(target_name: str, parent_fd: int) -> int:
    """Open a just-created target through its anchored parent."""
    return open_directory_fd(target_name, dir_fd=parent_fd)


def apply_created_target_mode(target_fd: int) -> None:
    os.fchmod(target_fd, OWNER_DIRECTORY_MODE)


def ensure_target_directory(target: Path, *, create: bool) -> bool:
    guard = current_target_guard(target)
    if guard is not None:
        revalidate_guard(guard, allow_missing=True)
        if guard.target_identity is not None:
            return True
        if not create:
            return False
        if guard.parent_fd is None:
            fail("target creation requires dir-fd filesystem support")
        try:
            os.mkdir(target.name, OWNER_DIRECTORY_MODE, dir_fd=guard.parent_fd)
        except FileExistsError:
            fail_concurrent("--target appeared concurrently during creation")
        guard.created_target = True
        try:
            created_info = os.stat(
                target.name,
                dir_fd=guard.parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            fail_concurrent("new --target disappeared before it could be opened")
        if stat.S_ISLNK(created_info.st_mode) or not stat.S_ISDIR(created_info.st_mode):
            fail_concurrent("new --target changed to an unsafe path before opening")
        created_identity = identity_of(created_info)
        guard.target_identity = created_identity
        opened_fd = open_created_target_directory(target.name, guard.parent_fd)
        opened_info = os.fstat(opened_fd)
        if identity_of(opened_info) != created_identity:
            os.close(opened_fd)
            fail_concurrent("new --target changed while it was being opened")
        guard.target_fd = opened_fd
        try:
            current_binding = os.stat(
                target.name,
                dir_fd=guard.parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            fail_concurrent("new --target binding disappeared after opening")
        if identity_of(current_binding) != created_identity:
            fail_concurrent("new --target binding changed after it was opened")
        try:
            if not hasattr(os, "fchmod"):
                fail("target creation requires descriptor-based chmod support")
            apply_created_target_mode(guard.target_fd)
            info = os.fstat(guard.target_fd)
            if identity_of(info) != guard.target_identity:
                fail_concurrent("new --target identity changed during mode application")
            validate_private_directory_info(info, "new --target")
            current_info = os.stat(
                target.name,
                dir_fd=guard.parent_fd,
                follow_symlinks=False,
            )
            if identity_of(current_info) != guard.target_identity:
                fail_concurrent("new --target binding changed during mode application")
            validate_private_directory_info(current_info, "new --target")
            revalidate_guard(guard, allow_missing=False)
        except BaseException:
            try:
                remove_created_target_if_empty(target)
            except ConcurrentTargetChange:
                # Preserve a target that another actor rebound during failure cleanup.
                pass
            raise
        return True

    try:
        info = target.lstat()
    except FileNotFoundError:
        if not create:
            return False
        fail("target creation requires an active anchored target lock")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail("--target must remain a real directory")
    return True


def target_entry_info(target: Path, name: str) -> os.stat_result | None:
    if not ensure_target_directory(target, create=False):
        return None
    guard = current_target_guard(target)
    try:
        if guard is not None and guard.target_fd is not None:
            return os.stat(name, dir_fd=guard.target_fd, follow_symlinks=False)
        return (target / name).lstat()
    except FileNotFoundError:
        return None


def target_entry_present(target: Path, name: str) -> bool:
    return target_entry_info(target, name) is not None


def read_file_at(
    directory_fd: int,
    name: str,
    label: str,
    *,
    owner_only: bool = False,
    max_bytes: int = MANAGED_PAYLOAD_MAX_BYTES,
) -> tuple[bytes, os.stat_result]:
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        fail(f"{label} must be a regular non-symlink file")
    if before.st_nlink != 1:
        fail(f"{label} must not have hard-link aliases")
    if owner_only and not is_owner_only_file(before):
        fail(f"{label} must be owned by the current user with mode 0600")
    require_bounded_size(before, label, max_bytes)

    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if identity_of(opened) != identity_of(before):
            fail_concurrent(f"{label} changed while it was being opened")
        require_bounded_size(opened, label, max_bytes)
        blocks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, 65536)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                fail(f"{label} exceeds the {max_bytes}-byte size limit")
            blocks.append(block)
        after = os.fstat(descriptor)
        require_bounded_size(after, label, max_bytes)
    finally:
        os.close(descriptor)
    try:
        final = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        fail_concurrent(f"{label} disappeared while it was being read")
    expected = identity_of(before)
    if identity_of(after) != expected or identity_of(final) != expected:
        fail_concurrent(f"{label} changed while it was being read")
    require_bounded_size(final, label, max_bytes)
    if owner_only and not is_owner_only_file(final):
        fail(f"{label} must be owned by the current user with mode 0600")
    return b"".join(blocks), final


def snapshot_file_at(
    directory_fd: int, name: str, label: str, *, owner_only: bool
) -> FileSnapshot | None:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    content, info = read_file_at(directory_fd, name, label, owner_only=owner_only)
    return FileSnapshot(
        identity=identity_of(info),
        digest=sha256_bytes(content),
        mode=stat.S_IMODE(info.st_mode),
        owner=owner_of(info),
    )


def snapshot_held_target_file(
    guard: TargetGuard,
    name: str,
    *,
    owner_only: bool,
) -> FileSnapshot | None:
    revalidate_guard_parent(guard)
    revalidate_held_target(guard)
    assert guard.target_fd is not None
    return snapshot_file_at(
        guard.target_fd,
        name,
        f"managed path {guard.target / name}",
        owner_only=owner_only,
    )


def read_target_file(
    target: Path,
    name: str,
    label: str,
    *,
    owner_only: bool = False,
    max_bytes: int = MANAGED_PAYLOAD_MAX_BYTES,
) -> tuple[bytes, os.stat_result]:
    guard = current_target_guard(target)
    if guard is None or guard.target_fd is None:
        return read_regular_file(
            target / name,
            label,
            owner_only=owner_only,
            max_bytes=max_bytes,
        )

    revalidate_guard(guard, allow_missing=False)
    return read_file_at(
        guard.target_fd,
        name,
        label,
        owner_only=owner_only,
        max_bytes=max_bytes,
    )


def snapshot_target_file(target: Path, name: str, *, owner_only: bool) -> FileSnapshot | None:
    info = target_entry_info(target, name)
    if info is None:
        return None
    content, final = read_target_file(
        target,
        name,
        f"managed path {target / name}",
        owner_only=owner_only,
    )
    return FileSnapshot(
        identity=identity_of(final),
        digest=sha256_bytes(content),
        mode=stat.S_IMODE(final.st_mode),
        owner=owner_of(final),
    )


def capture_managed_snapshot(
    target: Path, *, owner_only: bool = True
) -> dict[str, FileSnapshot | None]:
    return {
        name: snapshot_target_file(target, name, owner_only=owner_only)
        for name in (*MANAGED_FILES, STAMP_NAME)
    }


def assert_target_snapshot(target: Path, name: str, expected: FileSnapshot | None) -> None:
    actual = snapshot_target_file(target, name, owner_only=False)
    if actual != expected:
        fail_concurrent(f"managed path changed concurrently: {target / name}")


def assert_managed_snapshot(target: Path, expected: dict[str, FileSnapshot | None]) -> None:
    for name in (*MANAGED_FILES, STAMP_NAME):
        assert_target_snapshot(target, name, expected[name])


def target_has_instruction_override(target: Path) -> bool:
    return target_entry_present(target, OVERRIDE_NAME)


def reject_instruction_override(target: Path, action: str) -> None:
    if target_has_instruction_override(target):
        fail(f"{OVERRIDE_NAME} shadows the managed AGENTS.md; remove it before {action}")


def managed_path(target: Path, name: str) -> Path:
    path = target / name
    info = target_entry_info(target, name)
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            fail(f"managed path {path} must be a regular non-symlink file")
        if info.st_nlink != 1:
            fail(f"managed path {path} must not have hard-link aliases")
    return path


def validate_digest_map(value: Any, label: str) -> dict[str, str | None]:
    if not isinstance(value, dict) or set(value) != set(MANAGED_FILES):
        fail(f"{label} must declare exactly {list(MANAGED_FILES)}")
    result: dict[str, str | None] = {}
    for name in MANAGED_FILES:
        digest = value[name]
        if digest is not None and (
            not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest)
        ):
            fail(f"{label}.{name} must be null or a lowercase SHA-256 digest")
        result[name] = digest
    return result


def load_stamp(target: Path) -> dict[str, Any] | None:
    stamp_path = target / STAMP_NAME
    if target_entry_info(target, STAMP_NAME) is None:
        return None
    content, _ = read_target_file(
        target,
        STAMP_NAME,
        f"managed stamp {stamp_path}",
        max_bytes=METADATA_MAX_BYTES,
    )
    stamp = parse_json_object(content, f"managed stamp {stamp_path}")
    require_exact_keys(stamp, STAMP_KEYS, "managed stamp")
    if stamp["schema_version"] != 1 or stamp["product_name"] != PRODUCT_NAME:
        fail("managed stamp identity or schema is invalid")
    if not isinstance(stamp["build_version"], str) or not SEMVER_PATTERN.fullmatch(
        stamp["build_version"]
    ):
        fail("managed stamp build version is invalid")
    if not isinstance(stamp["setup_id"], str):
        fail("managed stamp setup_id must be a string")
    validate_setup_id(stamp["setup_id"])
    if stamp["canonical_target"] != str(target):
        fail("managed stamp is bound to a different canonical target")
    validate_digest_map(stamp["managed_files"], "managed stamp managed_files")
    return stamp


def config_base_intact(current: bytes, base: bytes) -> bool:
    """Return True when every managed base line is present verbatim in the
    current config.toml.

    config.toml is co-owned: the manager writes the setup base, and the Codex
    runtime appends project-trust decisions to it at launch as new
    ``[projects."<workspace>"]`` tables. The managed guarantee is scoped to the
    base ``key = value`` lines the manager owns -- each must survive verbatim --
    while the runtime's added lines are tolerated. A changed or removed base
    line reads as drift; AGENTS.md stays byte-exact because the runtime never
    writes it. Line-based rather than TOML-parsed so the manager keeps running
    on Pythons without ``tomllib`` (3.11+); the setup base is our own
    controlled, simple key/value text.
    """
    try:
        current_lines = set(current.decode("utf-8").splitlines())
        base_lines = base.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return False
    for line in base_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line not in current_lines:
            return False
    return True


# Runtime/user tables the Codex runtime or the operator may legitimately add to
# the co-owned config.toml after the managed base. Verified against Codex
# rust-v0.145.0: the runtime persists project trust into ``[projects.*]``, plugin
# enablement into ``[plugins.*]`` and marketplace metadata into
# ``[marketplaces.*]`` (config_toml.rs / edit.rs, via toml_edit); ``[mcp_servers.*]``
# is the operator's own MCP surface. Any other top-level table -- for example a
# ``[sandbox_workspace_write]`` that would flip ``network_access`` on, or a
# ``[model_providers.*]`` -- changes the managed permission posture, so it reads
# as drift rather than a tolerated co-owned addition. Codex itself is
# deny_unknown_fields and fails closed on duplicate keys/tables, so this check
# makes the manager's notion of "clean" agree with what Codex will actually load.
CONFIG_OVERLAY_TABLE_ROOTS = frozenset({"projects", "plugins", "marketplaces", "mcp_servers"})


def _toml_table_root(body: str) -> str | None:
    """Return the first dotted-key segment of a TOML table header body, or None
    when it cannot be parsed. ``projects."/abs/path"`` -> ``projects``;
    ``mcp_servers.name`` -> ``mcp_servers``; ``"quoted root"`` -> ``quoted root``."""
    root: list[str] = []
    quote = ""
    for char in body.strip():
        if quote:
            if char == quote:
                quote = ""
            else:
                root.append(char)
            continue
        if char in ('"', "'"):
            quote = char
            continue
        if char == ".":
            break
        if char.isspace():
            if root:
                break
            continue
        root.append(char)
    if quote:
        return None
    token = "".join(root)
    return token or None


def _toml_top_level(text: str) -> tuple[set[str], set[str]] | None:
    """Walk a TOML document line-by-line and return
    ``(top_level_bare_keys, table_roots)`` where top-level bare keys are those
    appearing before any table header. Returns None on a malformed table header.
    Line-based to match config_base_intact's parser-free contract (the managed
    base and the tolerated overlays are simple key/value and single-line tables);
    an unparseable shape fails closed as drift."""
    bare_keys: set[str] = set()
    table_roots: set[str] = set()
    seen_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            inner = line[1:]
            if inner.startswith("["):
                inner = inner[1:]
            close = inner.find("]")
            if close == -1:
                return None
            root = _toml_table_root(inner[:close])
            if root is None:
                return None
            table_roots.add(root)
            seen_table = True
            continue
        if not seen_table:
            key = line.split("=", 1)[0].strip().strip('"').strip("'")
            if key:
                bare_keys.add(key)
    return bare_keys, table_roots


def config_managed_intact(current: bytes, base: bytes) -> bool:
    """Return True when the current config.toml preserves the managed base *and*
    every addition beyond it belongs to an approved co-owned namespace. This is
    stricter than config_base_intact, which alone would read an injected
    ``[sandbox_workspace_write]`` or an extra top-level key as clean even though
    it silently changes the managed permission posture."""
    if not config_base_intact(current, base):
        return False
    try:
        current_text = current.decode("utf-8")
        base_text = base.decode("utf-8")
    except UnicodeDecodeError:
        return False
    current_structure = _toml_top_level(current_text)
    base_structure = _toml_top_level(base_text)
    if current_structure is None or base_structure is None:
        return False
    current_keys, current_tables = current_structure
    base_keys, base_tables = base_structure
    if not current_tables <= (base_tables | CONFIG_OVERLAY_TABLE_ROOTS):
        return False
    if not current_keys <= base_keys:
        return False
    return True


def preserve_config_overlays(current: bytes, new_base: bytes) -> bytes:
    """Return the new managed base with the current config.toml's approved
    co-owned overlay tables re-attached.

    Codex persists project trust, plugin enablement and marketplace metadata into
    config.toml at runtime (rust-v0.145.0, via toml_edit); the operator adds
    ``[mcp_servers.*]``; NDDev enables the builder there too. Rewriting the file to
    the pure setup base on update/switch -- the previous behaviour -- silently
    discarded all of it. Because a setup base is only top-level keys (setups
    declare no tables) and TOML requires top-level keys to precede any table, the
    overlay tail is exactly the text from the first top-level table header onward,
    so the split needs no record of the old base bytes. Callers must have already
    confirmed the target is clean (config_managed_intact), which guarantees the
    tail holds only approved namespaces; an undecodable file falls back to the
    pure base."""
    try:
        text = current.decode("utf-8")
    except UnicodeDecodeError:
        return new_base
    lines = text.splitlines(keepends=True)
    tail_start: int | None = None
    for index, raw in enumerate(lines):
        if raw.lstrip().startswith("["):
            tail_start = index
            break
    if tail_start is None:
        return new_base
    tail = "".join(lines[tail_start:])
    base_text = new_base.decode("utf-8")
    if not base_text.endswith("\n"):
        base_text += "\n"
    # Reproduce the single blank line the builder writer and Codex leave between
    # the base and the first overlay table (byte-stable for an unchanged update).
    separator = "" if tail.startswith("\n") else "\n"
    return (base_text + separator + tail).encode("utf-8")


def _config_base_intact_on_disk(target: Path, setup_id: object) -> bool:
    """Read the target's config.toml and confirm the managed base is intact,
    tolerating Codex's runtime additions. Any read/render failure is treated as
    drift (conservative)."""
    if not isinstance(setup_id, str):
        return False
    try:
        current, _ = read_target_file(
            target,
            "config.toml",
            f"managed path {target / 'config.toml'}",
            owner_only=False,
            max_bytes=MANAGED_PAYLOAD_MAX_BYTES,
        )
        _, rendered = render_setup(setup_id)
    except (SystemExit, OSError, ValueError):
        return False
    return config_managed_intact(current, rendered["config.toml"])


def inspect_target(target: Path) -> dict[str, Any]:
    if not ensure_target_directory(target, create=False):
        return {
            "state": "missing",
            "setup_id": None,
            "build_version": None,
            "drift": [],
            "unmanaged_managed_paths": [],
            "agents_override_present": False,
        }

    override_present = target_has_instruction_override(target)
    stamp = load_stamp(target)
    existing = []
    for name in MANAGED_FILES:
        managed_path(target, name)
        if target_entry_info(target, name) is not None:
            existing.append(name)
    if stamp is None:
        return {
            "state": "unmanaged",
            "setup_id": None,
            "build_version": None,
            "drift": [],
            "unmanaged_managed_paths": existing,
            "agents_override_present": override_present,
        }

    expected = validate_digest_map(stamp["managed_files"], "managed stamp managed_files")
    drift: list[str] = []
    for name in MANAGED_FILES:
        snapshot = snapshot_target_file(target, name, owner_only=False)
        if expected[name] is None or snapshot is None:
            drift.append(name)
            continue
        owner_matches = not hasattr(os, "geteuid") or snapshot.owner == os.geteuid()
        if snapshot.mode != OWNER_FILE_MODE or not owner_matches:
            drift.append(name)
            continue
        if snapshot.digest == expected[name]:
            continue
        # config.toml is co-owned: the Codex runtime persists [projects.*] trust
        # into it at launch. Tolerate additions that leave the managed base
        # intact; a damaged base, or any change to AGENTS.md, is still drift.
        if name == "config.toml" and _config_base_intact_on_disk(target, stamp["setup_id"]):
            continue
        drift.append(name)
    stamp_snapshot = snapshot_target_file(target, STAMP_NAME, owner_only=False)
    if stamp_snapshot is None:
        drift.append(STAMP_NAME)
    else:
        stamp_owner_matches = not hasattr(os, "geteuid") or stamp_snapshot.owner == os.geteuid()
        if stamp_snapshot.mode != OWNER_FILE_MODE or not stamp_owner_matches:
            drift.append(STAMP_NAME)
    return {
        "state": "managed",
        "setup_id": stamp["setup_id"],
        "build_version": stamp["build_version"],
        "drift": drift,
        "unmanaged_managed_paths": [],
        "agents_override_present": override_present,
    }


def require_clean_managed(target: Path) -> dict[str, Any]:
    status = inspect_target(target)
    if status["state"] != "managed":
        fail("target is not managed by nddev-codex-app")
    if status["drift"]:
        fail(f"managed target has drift: {', '.join(status['drift'])}")
    return status


def require_effective_clean_managed(target: Path) -> dict[str, Any]:
    status = require_clean_managed(target)
    reject_instruction_override(target, "launch")
    setup_id = status["setup_id"]
    if not isinstance(setup_id, str):
        fail("managed target does not declare a setup identity")
    _, rendered = render_setup(setup_id)
    expected = {
        **rendered,
        STAMP_NAME: stamp_bytes(target, setup_id, rendered),
    }
    for name, expected_content in expected.items():
        actual_content, _ = read_target_file(
            target,
            name,
            f"managed path {target / name}",
            owner_only=True,
            max_bytes=(METADATA_MAX_BYTES if name == STAMP_NAME else MANAGED_PAYLOAD_MAX_BYTES),
        )
        # config.toml is co-owned: tolerate the Codex runtime's [projects.*]
        # trust additions as long as the managed base is intact. AGENTS.md and
        # the stamp stay byte-exact.
        if name == "config.toml":
            intact = config_managed_intact(actual_content, expected_content)
        else:
            intact = actual_content == expected_content
        if not intact:
            fail(
                "managed target is not the current canonical catalog setup; "
                f"run apply --setup {setup_id} before launch"
            )
    return status


def stamp_bytes(target: Path, setup_id: str, rendered: dict[str, bytes]) -> bytes:
    return canonical_json(
        {
            "schema_version": 1,
            "product_name": PRODUCT_NAME,
            "build_version": VERSION,
            "setup_id": setup_id,
            "canonical_target": str(target),
            "managed_files": {name: sha256_bytes(rendered[name]) for name in MANAGED_FILES},
        }
    )


def backup_pool(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-codex-backups"


def lock_path(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-codex.lock"


@contextlib.contextmanager
def target_lock(target: Path) -> Iterator[TargetGuard]:
    if not anchored_directory_operations_supported():
        fail("mutating commands require dir-fd and no-follow filesystem support on this platform")

    lock = lock_path(target)
    parent_info = require_directory(target.parent, "canonical --target parent")
    parent_fd = open_directory_fd(target.parent)
    lock_fd: int | None = None
    target_fd: int | None = None
    lock_identity: PathIdentity | None = None
    lock_owner_snapshot: FileSnapshot | None = None
    guard: TargetGuard | None = None
    token: contextvars.Token[TargetGuard | None] | None = None
    operation_error: BaseException | None = None

    try:
        if identity_of(os.fstat(parent_fd)) != identity_of(parent_info):
            fail("canonical --target parent changed while acquiring the lock")
        try:
            os.stat(lock.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            fail(f"target is locked: {lock}")
        try:
            os.mkdir(lock.name, OWNER_DIRECTORY_MODE, dir_fd=parent_fd)
        except FileExistsError:
            fail(f"target is locked: {lock}")
        created_lock = os.stat(lock.name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(created_lock.st_mode) or not stat.S_ISDIR(created_lock.st_mode):
            fail("new target lock is not a real directory")
        lock_identity = identity_of(created_lock)
        lock_fd = open_directory_fd(lock.name, dir_fd=parent_fd)
        if identity_of(os.fstat(lock_fd)) != lock_identity:
            fail_concurrent("new target lock changed while it was being opened")
        os.fchmod(lock_fd, OWNER_DIRECTORY_MODE)

        try:
            target_info = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            target_identity = None
        else:
            if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISDIR(target_info.st_mode):
                fail("--target must remain a real directory")
            target_fd = open_directory_fd(target.name, dir_fd=parent_fd)
            opened_target = os.fstat(target_fd)
            if identity_of(opened_target) != identity_of(target_info):
                fail("--target changed while acquiring the lock")
            target_identity = identity_of(opened_target)

        guard = TargetGuard(
            target=target,
            parent_identity=identity_of(parent_info),
            target_identity=target_identity,
            parent_fd=parent_fd,
            target_fd=target_fd,
        )
        token = ACTIVE_TARGET_GUARD.set(guard)
        owner = canonical_json({"schema_version": 1, "pid": os.getpid(), "target": str(target)})
        write_new_file("owner.json", owner, dir_fd=lock_fd)
        lock_owner_snapshot = snapshot_file_at(
            lock_fd,
            "owner.json",
            "target lock owner",
            owner_only=True,
        )
        if lock_owner_snapshot is None:
            fail("target lock owner disappeared after creation")
        yield guard
    except BaseException as exc:
        operation_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        if token is not None:
            ACTIVE_TARGET_GUARD.reset(token)
        try:
            if lock_identity is not None:
                current_lock = os.stat(lock.name, dir_fd=parent_fd, follow_symlinks=False)
                if identity_of(current_lock) != lock_identity:
                    fail("target lock identity changed before cleanup")
                cleanup_lock_fd = lock_fd
                close_cleanup_lock_fd = False
                if cleanup_lock_fd is None:
                    cleanup_lock_fd = open_directory_fd(
                        lock.name,
                        dir_fd=parent_fd,
                    )
                    close_cleanup_lock_fd = True
                try:
                    if identity_of(os.fstat(cleanup_lock_fd)) != lock_identity:
                        fail("target lock handle changed before cleanup")
                    try:
                        owner_info = os.stat(
                            "owner.json",
                            dir_fd=cleanup_lock_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        if lock_owner_snapshot is not None:
                            fail("target lock owner disappeared before cleanup")
                    else:
                        if (
                            stat.S_ISLNK(owner_info.st_mode)
                            or not stat.S_ISREG(owner_info.st_mode)
                            or owner_info.st_nlink != 1
                        ):
                            fail("target lock owner entry changed before cleanup")
                        current_owner = snapshot_file_at(
                            cleanup_lock_fd,
                            "owner.json",
                            "target lock owner",
                            owner_only=True,
                        )
                        if current_owner != lock_owner_snapshot:
                            fail("target lock owner content changed before cleanup")
                        os.unlink("owner.json", dir_fd=cleanup_lock_fd)
                    if os.listdir(cleanup_lock_fd):
                        fail("target lock contains unexpected entries before cleanup")
                    os.fsync(cleanup_lock_fd)
                finally:
                    if close_cleanup_lock_fd:
                        os.close(cleanup_lock_fd)
                os.rmdir(lock.name, dir_fd=parent_fd)
        except Exception as exc:
            cleanup_error = exc
        finally:
            if guard is not None:
                if guard.target_fd is not None:
                    os.close(guard.target_fd)
                    guard.target_fd = None
            elif target_fd is not None:
                os.close(target_fd)
            if lock_fd is not None:
                os.close(lock_fd)
            os.close(parent_fd)
        if cleanup_error is not None:
            if operation_error is None:
                raise CodexSetupError(
                    f"operation completed but target lock cleanup failed: {lock}"
                ) from cleanup_error
            raise CodexSetupError(
                "operation failed and target lock cleanup also failed: "
                f"{type(operation_error).__name__}: {operation_error}"
            ) from cleanup_error


def ensure_pool(target: Path) -> BackupPoolLease:
    guard = current_target_guard(target)
    if guard is None or guard.parent_fd is None:
        fail("backup mutation requires an active anchored target lock")
    revalidate_guard(guard, allow_missing=guard.target_identity is None)
    pool = backup_pool(target)
    created = False
    try:
        info = os.stat(pool.name, dir_fd=guard.parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.mkdir(pool.name, OWNER_DIRECTORY_MODE, dir_fd=guard.parent_fd)
        info = os.stat(pool.name, dir_fd=guard.parent_fd, follow_symlinks=False)
        created = True
    else:
        if stat.S_ISLNK(info.st_mode):
            fail(f"backup pool is unsafe: {pool}")
        validate_private_directory_info(info, "backup pool")
    pool_fd = open_directory_fd(pool.name, dir_fd=guard.parent_fd)
    try:
        pool_identity = identity_of(info)
        if identity_of(os.fstat(pool_fd)) != pool_identity:
            fail_concurrent("backup pool changed while it was being opened")
        if created:
            os.fchmod(pool_fd, OWNER_DIRECTORY_MODE)
        validate_private_directory_info(os.fstat(pool_fd), "backup pool")
        revalidate_directory_binding(
            guard.parent_fd,
            pool.name,
            pool_fd,
            pool_identity,
            "backup pool",
        )
        os.fsync(pool_fd)
    except BaseException:
        os.close(pool_fd)
        raise
    return BackupPoolLease(
        target=target,
        pool=pool,
        fd=pool_fd,
        identity=pool_identity,
    )


def entry_exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def remove_backup_directory_at(
    pool_fd: int,
    name: str,
    expected_identity: PathIdentity,
) -> None:
    try:
        before = os.stat(name, dir_fd=pool_fd, follow_symlinks=False)
    except FileNotFoundError:
        fail_concurrent(f"backup cleanup entry disappeared: {name}")
    if identity_of(before) != expected_identity:
        fail_concurrent(f"backup cleanup entry changed: {name}")
    cleanup_name = f".cleanup-{secrets.token_hex(8)}"
    anchored_rename(
        name,
        cleanup_name,
        source_fd=pool_fd,
        destination_fd=pool_fd,
    )
    moved = os.stat(cleanup_name, dir_fd=pool_fd, follow_symlinks=False)
    if identity_of(moved) != expected_identity:
        if not entry_exists_at(pool_fd, name):
            anchored_rename(
                cleanup_name,
                name,
                source_fd=pool_fd,
                destination_fd=pool_fd,
            )
        fail_concurrent(f"backup cleanup entry changed during quarantine: {name}")
    slot_fd = open_directory_fd(cleanup_name, dir_fd=pool_fd)
    try:
        if identity_of(os.fstat(slot_fd)) != expected_identity:
            fail_concurrent(f"backup cleanup handle changed: {name}")
        slot_entries = set(os.listdir(slot_fd))
        if not slot_entries <= {BACKUP_NAME, "payload"}:
            fail(f"backup cleanup encountered unexpected entries: {name}")
        if "payload" in slot_entries:
            payload_fd = open_directory_fd("payload", dir_fd=slot_fd)
            try:
                allowed_payload = {*MANAGED_FILES, STAMP_NAME}
                payload_entries = set(os.listdir(payload_fd))
                if not payload_entries <= allowed_payload:
                    fail(f"backup cleanup encountered unexpected payload entries: {name}")
                for payload_entry in payload_entries:
                    payload_info = os.stat(
                        payload_entry,
                        dir_fd=payload_fd,
                        follow_symlinks=False,
                    )
                    if stat.S_ISLNK(payload_info.st_mode) or not stat.S_ISREG(payload_info.st_mode):
                        fail(f"backup cleanup encountered an unsafe payload entry: {payload_entry}")
            finally:
                os.close(payload_fd)
        if BACKUP_NAME in slot_entries:
            envelope_info = os.stat(
                BACKUP_NAME,
                dir_fd=slot_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(envelope_info.st_mode) or not stat.S_ISREG(envelope_info.st_mode):
                fail("backup cleanup encountered an unsafe envelope")
        for entry in os.listdir(slot_fd):
            info = os.stat(entry, dir_fd=slot_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                payload_fd = open_directory_fd(entry, dir_fd=slot_fd)
                try:
                    for payload_entry in os.listdir(payload_fd):
                        payload_info = os.stat(
                            payload_entry,
                            dir_fd=payload_fd,
                            follow_symlinks=False,
                        )
                        if stat.S_ISDIR(payload_info.st_mode) and not stat.S_ISLNK(
                            payload_info.st_mode
                        ):
                            fail(
                                "backup cleanup encountered an unexpected nested "
                                f"directory: {payload_entry}"
                            )
                        os.unlink(payload_entry, dir_fd=payload_fd)
                    os.fsync(payload_fd)
                finally:
                    os.close(payload_fd)
                os.rmdir(entry, dir_fd=slot_fd)
            else:
                os.unlink(entry, dir_fd=slot_fd)
        os.fsync(slot_fd)
    finally:
        os.close(slot_fd)
    current = os.stat(cleanup_name, dir_fd=pool_fd, follow_symlinks=False)
    if identity_of(current) != expected_identity:
        fail_concurrent(f"backup cleanup quarantine changed: {name}")
    os.rmdir(cleanup_name, dir_fd=pool_fd)


def write_new_file(
    path: Path | str,
    content: bytes,
    mode: int = OWNER_FILE_MODE,
    *,
    dir_fd: int | None = None,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if dir_fd is None:
        descriptor = os.open(path, flags, mode)
    else:
        descriptor = os.open(path, flags, mode, dir_fd=dir_fd)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            fail(f"new file is unsafe: {path}")
        if mode == OWNER_FILE_MODE and not is_owner_only_file(info):
            fail(f"new file does not have owner-only mode 0600: {path}")
    finally:
        os.close(descriptor)


def remove_created_target_if_empty(target: Path) -> bool:
    guard = current_target_guard(target)
    if guard is None or not guard.created_target or guard.target_fd is None:
        return False
    revalidate_guard(guard, allow_missing=False)
    if os.listdir(guard.target_fd):
        return False
    os.close(guard.target_fd)
    guard.target_fd = None
    if guard.parent_fd is None:
        fail("target cleanup lost its anchored parent handle")
    try:
        os.rmdir(target.name, dir_fd=guard.parent_fd)
    except OSError:
        guard.target_fd = open_directory_fd(target.name, dir_fd=guard.parent_fd)
        raise
    guard.target_identity = None
    guard.created_target = False
    return True


@contextlib.contextmanager
def anchored_stage(
    target: Path,
    prefix: str,
    *,
    allow_detached_target: bool = False,
) -> Iterator[tuple[str, int]]:
    guard = current_target_guard(target)
    if guard is None or guard.parent_fd is None:
        fail("staging requires an active anchored target lock")
    if allow_detached_target:
        revalidate_guard_parent(guard)
        revalidate_held_target(guard)
    else:
        revalidate_guard(guard, allow_missing=guard.target_identity is None)
    stage_name = ""
    for _ in range(32):
        candidate = f".{target.name}.{prefix}-{secrets.token_hex(8)}"
        try:
            os.mkdir(candidate, OWNER_DIRECTORY_MODE, dir_fd=guard.parent_fd)
        except FileExistsError:
            continue
        stage_name = candidate
        break
    if not stage_name:
        fail("cannot allocate a unique anchored staging directory")
    stage_fd = open_directory_fd(stage_name, dir_fd=guard.parent_fd)
    os.fchmod(stage_fd, OWNER_DIRECTORY_MODE)
    cleanup_error: BaseException | None = None
    try:
        yield stage_name, stage_fd
    finally:
        try:
            for entry in os.listdir(stage_fd):
                info = os.stat(entry, dir_fd=stage_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    fail(f"staging directory contains unexpected directory: {entry}")
                os.unlink(entry, dir_fd=stage_fd)
            os.fsync(stage_fd)
            os.rmdir(stage_name, dir_fd=guard.parent_fd)
        except Exception as exc:
            cleanup_error = exc
        finally:
            os.close(stage_fd)
        if cleanup_error is not None:
            raise CodexSetupError(
                f"anchored staging cleanup failed: {target.parent / stage_name}"
            ) from cleanup_error


def quarantine_expected_target_entry(
    target: Path,
    name: str,
    expected: FileSnapshot,
    stage_fd: int,
    *,
    allow_detached_target: bool,
) -> None:
    guard = current_target_guard(target)
    if guard is None or guard.target_fd is None:
        fail("quarantine requires an active anchored target lock")
    if allow_detached_target:
        revalidate_guard_parent(guard)
        revalidate_held_target(guard)
    else:
        revalidate_guard(guard, allow_missing=False)
    quarantine_name = f"old-{name}"
    try:
        anchored_rename(
            name,
            quarantine_name,
            source_fd=guard.target_fd,
            destination_fd=stage_fd,
        )
    except FileNotFoundError:
        fail_concurrent(f"managed path disappeared before quarantine: {target / name}")
    try:
        moved = snapshot_file_at(
            stage_fd,
            quarantine_name,
            f"quarantined managed path {name}",
            owner_only=False,
        )
        if moved != expected:
            fail_concurrent(f"managed path changed before quarantine: {target / name}")
    except BaseException as exc:
        try:
            try:
                os.stat(name, dir_fd=guard.target_fd, follow_symlinks=False)
            except FileNotFoundError:
                anchored_rename(
                    quarantine_name,
                    name,
                    source_fd=stage_fd,
                    destination_fd=guard.target_fd,
                )
        except BaseException as recovery_error:
            raise CodexSetupError(
                f"concurrent change recovery failed for {target / name}"
            ) from recovery_error
        if isinstance(exc, ConcurrentTargetChange):
            raise
        raise ConcurrentTargetChange(
            f"managed path changed before quarantine: {target / name}"
        ) from exc
    guard.mutated_paths.add(name)


def replace_managed_state(
    target: Path,
    desired: dict[str, bytes | None],
    expected: dict[str, FileSnapshot | None],
    *,
    names: tuple[str, ...] = (*MANAGED_FILES, STAMP_NAME),
    allow_detached_target: bool = False,
) -> None:
    guard = current_target_guard(target)
    if guard is None or guard.target_fd is None:
        fail("managed-state replacement requires an active anchored target lock")
    allowed_names = set((*MANAGED_FILES, STAMP_NAME, BUILDER_PROFILE_NAME))
    if not names or len(set(names)) != len(names) or not set(names) <= allowed_names:
        fail("managed-state replacement received an invalid path selection")
    if allow_detached_target:
        revalidate_guard_parent(guard)
        revalidate_held_target(guard)
    else:
        revalidate_guard(guard, allow_missing=False)
    for name in names:
        actual = snapshot_held_target_file(guard, name, owner_only=False)
        if actual != expected[name]:
            fail_concurrent(f"managed path changed concurrently: {target / name}")
    with anchored_stage(
        target,
        "nddev-stage",
        allow_detached_target=allow_detached_target,
    ) as (_, stage_fd):
        staged: dict[str, FileSnapshot] = {}
        for name in names:
            content = desired[name]
            if content is not None:
                write_new_file(f"new-{name}", content, dir_fd=stage_fd)
                snapshot = snapshot_file_at(
                    stage_fd,
                    f"new-{name}",
                    f"staged managed path {name}",
                    owner_only=True,
                )
                if snapshot is None:
                    fail(f"staged managed path disappeared: {name}")
                staged[name] = snapshot
        for name in names:
            if allow_detached_target:
                revalidate_guard_parent(guard)
                revalidate_held_target(guard)
            else:
                revalidate_guard(guard, allow_missing=False)
            old = expected[name]
            if old is not None:
                quarantine_expected_target_entry(
                    target,
                    name,
                    old,
                    stage_fd,
                    allow_detached_target=allow_detached_target,
                )
                guard.manager_results[name] = None
            elif entry_exists_at(guard.target_fd, name):
                fail_concurrent(f"managed path appeared before replacement: {target / name}")

            content = desired[name]
            if content is None:
                continue
            try:
                os.link(
                    f"new-{name}",
                    name,
                    src_dir_fd=stage_fd,
                    dst_dir_fd=guard.target_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                fail_concurrent(f"managed path appeared during replacement: {target / name}")
            os.unlink(f"new-{name}", dir_fd=stage_fd)
            guard.mutated_paths.add(name)
            guard.manager_results[name] = staged[name]
            installed = snapshot_held_target_file(guard, name, owner_only=True)
            if installed != staged[name]:
                fail(f"postcondition failed for managed path: {name}")
        for name in names:
            content = desired[name]
            installed = snapshot_held_target_file(guard, name, owner_only=False)
            if content is None:
                if installed is not None:
                    fail_concurrent(f"managed path appeared after removal: {target / name}")
            elif installed is None or installed.digest != sha256_bytes(content):
                fail_concurrent(f"managed path changed after replacement: {target / name}")
        os.fsync(stage_fd)
        os.fsync(guard.target_fd)
        if not allow_detached_target:
            revalidate_guard(guard, allow_missing=False)


def validate_private_directory_info(info: os.stat_result, label: str) -> None:
    owner_matches = not hasattr(os, "geteuid") or owner_of(info) == os.geteuid()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != OWNER_DIRECTORY_MODE
        or not owner_matches
    ):
        fail(f"{label} must be owned by the current user with mode 0700")


def open_private_directory_at(
    parent_fd: int,
    name: str,
    label: str,
) -> tuple[int, os.stat_result]:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(before.st_mode):
        fail(f"{label} must be a real directory")
    validate_private_directory_info(before, label)
    descriptor = open_directory_fd(name, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if identity_of(opened) != identity_of(before):
            fail_concurrent(f"{label} changed while it was being opened")
        validate_private_directory_info(opened, label)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def revalidate_directory_binding(
    parent_fd: int,
    name: str,
    descriptor: int,
    expected_identity: PathIdentity,
    label: str,
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        fail_concurrent(f"{label} disappeared during validation")
    if (
        identity_of(current) != expected_identity
        or identity_of(os.fstat(descriptor)) != expected_identity
    ):
        fail_concurrent(f"{label} changed during validation")
    validate_private_directory_info(current, label)
    validate_private_directory_info(os.fstat(descriptor), label)


def validate_backup_envelope(
    envelope: dict[str, Any],
    slot: int,
    expected_target: Path,
) -> tuple[dict[str, str | None], str | None]:
    require_exact_keys(envelope, BACKUP_KEYS, f"backup slot {slot}")
    if (
        envelope["schema_version"] != 1
        or envelope["product_name"] != PRODUCT_NAME
        or envelope["slot"] != slot
    ):
        fail(f"backup slot {slot} identity or schema is invalid")
    if not isinstance(envelope["build_version"], str) or not SEMVER_PATTERN.fullmatch(
        envelope["build_version"]
    ):
        fail(f"backup slot {slot} build version is invalid")
    if envelope["canonical_target"] != str(expected_target):
        fail(f"backup slot {slot} is bound to a different canonical target")
    source_setup = envelope["source_setup_id"]
    if source_setup is not None:
        if not isinstance(source_setup, str):
            fail(f"backup slot {slot} source_setup_id is invalid")
        validate_setup_id(source_setup)
    digests = validate_digest_map(envelope["managed_files"], f"backup slot {slot}")
    stamp_digest = envelope["stamp_sha256"]
    if stamp_digest is not None and (
        not isinstance(stamp_digest, str) or not SHA256_PATTERN.fullmatch(stamp_digest)
    ):
        fail(f"backup slot {slot} stamp_sha256 must be null or a SHA-256 digest")
    if (source_setup is None) != (stamp_digest is None):
        fail(f"backup slot {slot} setup identity and stamp digest disagree")
    has_all_managed_files = all(digests[name] is not None for name in MANAGED_FILES)
    if (source_setup is not None) != has_all_managed_files:
        fail(f"backup slot {slot} managed payload and setup identity disagree")
    return digests, stamp_digest


def load_backup_from_pool_fd(
    target: Path,
    pool_fd: int,
    slot: int,
    *,
    entry_name: str | None = None,
) -> tuple[dict[str, Any], dict[str, bytes | None], int]:
    directory_name = entry_name if entry_name is not None else str(slot)
    slot_fd, slot_info = open_private_directory_at(
        pool_fd,
        directory_name,
        f"backup slot {slot}",
    )
    try:
        slot_identity = identity_of(slot_info)
        revalidate_directory_binding(
            pool_fd,
            directory_name,
            slot_fd,
            slot_identity,
            f"backup slot {slot}",
        )
        if set(os.listdir(slot_fd)) != {BACKUP_NAME, "payload"}:
            fail(f"backup slot {slot} contains unexpected entries")
        envelope_content, _ = read_file_at(
            slot_fd,
            BACKUP_NAME,
            f"backup slot {slot}",
            owner_only=True,
            max_bytes=METADATA_MAX_BYTES,
        )
        envelope = parse_json_object(envelope_content, f"backup slot {slot}")
        digests, stamp_digest = validate_backup_envelope(envelope, slot, target)
        payload_fd, payload_info = open_private_directory_at(
            slot_fd,
            "payload",
            f"backup slot {slot} payload",
        )
        try:
            payload_identity = identity_of(payload_info)
            revalidate_directory_binding(
                slot_fd,
                "payload",
                payload_fd,
                payload_identity,
                f"backup slot {slot} payload",
            )
            desired: dict[str, bytes | None] = {name: None for name in (*MANAGED_FILES, STAMP_NAME)}
            allowed_payload = {name for name, digest in digests.items() if digest is not None}
            if stamp_digest is not None:
                allowed_payload.add(STAMP_NAME)
            if set(os.listdir(payload_fd)) != allowed_payload:
                fail(f"backup slot {slot} contains unexpected payload entries")
            for name, digest in digests.items():
                if digest is None:
                    continue
                content, _ = read_file_at(
                    payload_fd,
                    name,
                    f"backup slot {slot} payload {name}",
                    owner_only=True,
                )
                if sha256_bytes(content) != digest:
                    fail(f"backup slot {slot} payload digest mismatch for {name}")
                desired[name] = content
            if stamp_digest is not None:
                stamp_content, _ = read_file_at(
                    payload_fd,
                    STAMP_NAME,
                    f"backup slot {slot} managed stamp",
                    owner_only=True,
                    max_bytes=METADATA_MAX_BYTES,
                )
                if sha256_bytes(stamp_content) != stamp_digest:
                    fail(f"backup slot {slot} managed stamp digest mismatch")
                desired[STAMP_NAME] = stamp_content
            if set(os.listdir(payload_fd)) != allowed_payload:
                fail(f"backup slot {slot} payload changed during validation")
            revalidate_directory_binding(
                slot_fd,
                "payload",
                payload_fd,
                payload_identity,
                f"backup slot {slot} payload",
            )
        finally:
            os.close(payload_fd)
        if set(os.listdir(slot_fd)) != {BACKUP_NAME, "payload"}:
            fail(f"backup slot {slot} changed during validation")
        revalidate_directory_binding(
            pool_fd,
            directory_name,
            slot_fd,
            slot_identity,
            f"backup slot {slot}",
        )
        slot_mtime_ns = os.fstat(slot_fd).st_mtime_ns
    finally:
        os.close(slot_fd)
    return envelope, desired, slot_mtime_ns


def open_backup_pool_lease(target: Path) -> BackupPoolLease:
    guard = current_target_guard(target)
    if guard is None or guard.parent_fd is None:
        fail("backup access requires an active anchored target lock")
    revalidate_guard(guard, allow_missing=guard.target_identity is None)
    pool = backup_pool(target)
    pool_fd, pool_info = open_private_directory_at(
        guard.parent_fd,
        pool.name,
        "backup pool",
    )
    return BackupPoolLease(
        target=target,
        pool=pool,
        fd=pool_fd,
        identity=identity_of(pool_info),
    )


def revalidate_backup_pool(
    target: Path,
    pool_fd: int,
    expected_identity: PathIdentity | None = None,
) -> None:
    guard = current_target_guard(target)
    if guard is None or guard.parent_fd is None:
        fail("backup access requires an active anchored target lock")
    revalidate_guard_parent(guard)
    if guard.target_fd is not None:
        revalidate_held_target(guard)
    if expected_identity is None:
        expected_identity = identity_of(os.fstat(pool_fd))
    revalidate_directory_binding(
        guard.parent_fd,
        backup_pool(target).name,
        pool_fd,
        expected_identity,
        "backup pool",
    )


def revalidate_backup_pool_lease(lease: BackupPoolLease) -> None:
    if lease.closed:
        fail("backup pool lease is already closed")
    revalidate_backup_pool(lease.target, lease.fd, lease.identity)


def close_backup_pool_lease(lease: BackupPoolLease) -> None:
    if lease.closed:
        return
    os.close(lease.fd)
    lease.closed = True


def validate_backup_before_target_mutation(lease: BackupPoolLease) -> None:
    revalidate_backup_pool_lease(lease)


def choose_backup_slot(
    pool_fd: int,
    target: Path,
    exclude: int | None = None,
) -> BackupSlotChoice:
    candidates: list[tuple[int, int, PathIdentity]] = []
    for slot in range(10):
        try:
            slot_info = os.stat(str(slot), dir_fd=pool_fd, follow_symlinks=False)
        except FileNotFoundError:
            return BackupSlotChoice(slot=slot, expected_identity=None)
        slot_identity = identity_of(slot_info)
        envelope, _, mtime_ns = load_backup_from_pool_fd(target, pool_fd, slot)
        current = os.stat(str(slot), dir_fd=pool_fd, follow_symlinks=False)
        if identity_of(current) != slot_identity:
            fail_concurrent(f"backup slot {slot} changed during selection")
        candidates.append((mtime_ns, envelope["slot"], slot_identity))
    eligible = [item for item in candidates if item[1] != exclude]
    if not eligible:
        fail("no backup slot is available without destroying the restore source")
    _, slot, slot_identity = min(eligible)
    return BackupSlotChoice(slot=slot, expected_identity=slot_identity)


def revalidate_backup_slot_choice(
    pool_fd: int,
    choice: BackupSlotChoice,
) -> None:
    try:
        current = os.stat(
            str(choice.slot),
            dir_fd=pool_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if choice.expected_identity is None:
            return
        fail_concurrent(f"backup slot {choice.slot} disappeared after selection")
    if choice.expected_identity is None:
        fail_concurrent(f"backup slot {choice.slot} appeared after selection")
    if identity_of(current) != choice.expected_identity:
        fail_concurrent(f"backup slot {choice.slot} changed after selection")


def validate_staged_backup_before_publish(
    lease: BackupPoolLease,
    choice: BackupSlotChoice,
    staging_name: str,
    staging_identity: PathIdentity,
) -> None:
    revalidate_backup_pool_lease(lease)
    revalidate_backup_slot_choice(lease.fd, choice)
    current = os.stat(
        staging_name,
        dir_fd=lease.fd,
        follow_symlinks=False,
    )
    if identity_of(current) != staging_identity:
        fail_concurrent("staged backup changed before publication")


def _create_transaction_backup_with_lease(
    target: Path,
    exclude: int | None = None,
    *,
    lease: BackupPoolLease,
) -> tuple[int, dict[str, bytes | None]]:
    status = inspect_target(target)
    setup_id = status["setup_id"] if status["state"] == "managed" else None
    guard = current_target_guard(target)
    if guard is None or guard.parent_fd is None:
        fail("backup mutation requires an active anchored target lock")
    if lease.target != target:
        fail("backup pool lease is bound to a different target")
    revalidate_backup_pool_lease(lease)
    pool = lease.pool
    pool_fd = lease.fd
    choice = choose_backup_slot(pool_fd, target, exclude=exclude)
    revalidate_backup_pool_lease(lease)
    revalidate_backup_slot_choice(pool_fd, choice)
    slot = choice.slot
    destination_name = str(slot)
    hold_name = f".{slot}.replaced"
    staging_name = f".{slot}.new-{secrets.token_hex(8)}"
    destination = pool / destination_name
    old_identity = choice.expected_identity
    old_quarantined = False
    staging_identity: PathIdentity | None = None
    installed_new = False
    try:
        if entry_exists_at(pool_fd, hold_name):
            fail(f"backup recovery hold already exists: {pool / hold_name}")
        desired: dict[str, bytes | None] = {name: None for name in (*MANAGED_FILES, STAMP_NAME)}
        digests: dict[str, str | None] = {}
        for name in MANAGED_FILES:
            if target_entry_present(target, name):
                content, _ = read_target_file(
                    target,
                    name,
                    f"managed path {target / name}",
                    owner_only=True,
                )
                desired[name] = content
                digests[name] = sha256_bytes(content)
            else:
                digests[name] = None
        stamp_digest: str | None = None
        if target_entry_present(target, STAMP_NAME):
            stamp_content, _ = read_target_file(
                target,
                STAMP_NAME,
                f"managed stamp {target / STAMP_NAME}",
                owner_only=True,
                max_bytes=METADATA_MAX_BYTES,
            )
            desired[STAMP_NAME] = stamp_content
            stamp_digest = sha256_bytes(stamp_content)
        envelope = {
            "schema_version": 1,
            "product_name": PRODUCT_NAME,
            "build_version": VERSION,
            "slot": slot,
            "canonical_target": str(target),
            "source_setup_id": setup_id,
            "managed_files": digests,
            "stamp_sha256": stamp_digest,
        }

        os.mkdir(staging_name, OWNER_DIRECTORY_MODE, dir_fd=pool_fd)
        staging_info = os.stat(
            staging_name,
            dir_fd=pool_fd,
            follow_symlinks=False,
        )
        staging_identity = identity_of(staging_info)
        staging_fd = open_directory_fd(staging_name, dir_fd=pool_fd)
        try:
            if identity_of(os.fstat(staging_fd)) != staging_identity:
                fail_concurrent("backup staging directory changed while opening")
            os.fchmod(staging_fd, OWNER_DIRECTORY_MODE)
            validate_private_directory_info(
                os.fstat(staging_fd),
                f"backup staging slot {slot}",
            )
            os.mkdir("payload", OWNER_DIRECTORY_MODE, dir_fd=staging_fd)
            payload_fd = open_directory_fd("payload", dir_fd=staging_fd)
            try:
                os.fchmod(payload_fd, OWNER_DIRECTORY_MODE)
                validate_private_directory_info(
                    os.fstat(payload_fd),
                    f"backup slot {slot} payload",
                )
                for name, content in desired.items():
                    if content is not None:
                        write_new_file(name, content, dir_fd=payload_fd)
                os.fsync(payload_fd)
            finally:
                os.close(payload_fd)
            write_new_file(BACKUP_NAME, canonical_json(envelope), dir_fd=staging_fd)
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)
        _, validated_desired, _ = load_backup_from_pool_fd(
            target,
            pool_fd,
            slot,
            entry_name=staging_name,
        )
        if validated_desired != desired:
            fail("staged backup content does not match the captured target state")
        if staging_identity is None:
            fail("backup staging identity was not captured")
        validate_staged_backup_before_publish(
            lease,
            choice,
            staging_name,
            staging_identity,
        )
        if old_identity is not None:
            anchored_rename(
                destination_name,
                hold_name,
                source_fd=pool_fd,
                destination_fd=pool_fd,
            )
            moved = os.stat(hold_name, dir_fd=pool_fd, follow_symlinks=False)
            if identity_of(moved) != old_identity:
                if not entry_exists_at(pool_fd, destination_name):
                    anchored_rename(
                        hold_name,
                        destination_name,
                        source_fd=pool_fd,
                        destination_fd=pool_fd,
                    )
                fail_concurrent(f"backup slot changed during rotation: {destination}")
            old_quarantined = True
        if entry_exists_at(pool_fd, destination_name):
            fail_concurrent(f"backup slot {slot} appeared during installation")
        anchored_rename(
            staging_name,
            destination_name,
            source_fd=pool_fd,
            destination_fd=pool_fd,
        )
        installed_new = True
        installed = os.stat(
            destination_name,
            dir_fd=pool_fd,
            follow_symlinks=False,
        )
        if identity_of(installed) != staging_identity:
            fail_concurrent(f"new backup slot identity mismatch: {destination}")
        os.fsync(pool_fd)
        _, published_desired, _ = load_backup_from_pool_fd(target, pool_fd, slot)
        if published_desired != validated_desired:
            fail("published backup content changed during installation")
        revalidate_backup_pool_lease(lease)
        if old_identity is not None:
            held = os.stat(hold_name, dir_fd=pool_fd, follow_symlinks=False)
            if identity_of(held) != old_identity:
                fail_concurrent(f"backup recovery hold changed: {pool / hold_name}")
            remove_backup_directory_at(pool_fd, hold_name, old_identity)
            old_quarantined = False
        os.fsync(pool_fd)
    except BaseException:
        if installed_new and staging_identity is not None:
            try:
                current = os.stat(
                    destination_name,
                    dir_fd=pool_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                if identity_of(current) != staging_identity:
                    fail_concurrent(f"new backup slot changed during recovery: {destination}")
                remove_backup_directory_at(
                    pool_fd,
                    destination_name,
                    staging_identity,
                )
        elif staging_identity is not None and entry_exists_at(pool_fd, staging_name):
            remove_backup_directory_at(
                pool_fd,
                staging_name,
                staging_identity,
            )
        if old_quarantined and old_identity is not None and entry_exists_at(pool_fd, hold_name):
            if entry_exists_at(pool_fd, destination_name):
                fail("cannot restore rotated backup because its slot is occupied")
            held = os.stat(hold_name, dir_fd=pool_fd, follow_symlinks=False)
            if identity_of(held) != old_identity:
                fail_concurrent("backup recovery hold identity changed")
            anchored_rename(
                hold_name,
                destination_name,
                source_fd=pool_fd,
                destination_fd=pool_fd,
            )
        raise
    return slot, validated_desired


def create_transaction_backup(
    target: Path,
    exclude: int | None = None,
    *,
    lease: BackupPoolLease | None = None,
) -> tuple[int, dict[str, bytes | None], BackupPoolLease]:
    owns_lease = lease is None
    if lease is None:
        lease = ensure_pool(target)
    try:
        slot, desired = _create_transaction_backup_with_lease(
            target,
            exclude,
            lease=lease,
        )
    except BaseException:
        if owns_lease:
            close_backup_pool_lease(lease)
        raise
    return slot, desired, lease


def apply_rendered(
    target: Path,
    setup_id: str,
    rendered: dict[str, bytes],
    *,
    config_payload: bytes | None = None,
) -> None:
    guard = current_target_guard(target)
    if guard is None:
        fail("apply requires an active anchored target lock")
    expected = guard.expected_managed
    if expected is None:
        expected = capture_managed_snapshot(target)
    ensure_target_directory(target, create=True)
    # The stamp digests the pure setup base (``rendered``); the on-disk config may
    # additionally carry approved co-owned overlays via ``config_payload``, which
    # inspect_target tolerates as long as config_managed_intact holds.
    desired: dict[str, bytes | None] = {
        **rendered,
        STAMP_NAME: stamp_bytes(target, setup_id, rendered),
    }
    if config_payload is not None:
        desired["config.toml"] = config_payload
    replace_managed_state(target, desired, expected)


def plan_setup(target: Path, setup_id: str) -> dict[str, Any]:
    _, rendered = render_setup(setup_id)
    status = inspect_target(target)
    if status["agents_override_present"]:
        reject_instruction_override(target, "plan, apply, switch, restore, or launch")
    if status["state"] == "unmanaged" and status["unmanaged_managed_paths"]:
        fail(
            "unmanaged target already contains managed paths: "
            + ", ".join(status["unmanaged_managed_paths"])
        )
    if status["state"] == "managed" and status["drift"]:
        fail(f"managed target has drift: {', '.join(status['drift'])}")
    if status["state"] in {"missing", "unmanaged"}:
        operation = "install"
    elif status["setup_id"] == setup_id:
        operation = "update"
    else:
        operation = "switch"

    changes = []
    for name in MANAGED_FILES:
        desired = sha256_bytes(rendered[name])
        current_snapshot = snapshot_target_file(target, name, owner_only=False)
        current = current_snapshot.digest if current_snapshot is not None else None
        if current != desired:
            changes.append(name)
    desired_stamp = sha256_bytes(stamp_bytes(target, setup_id, rendered))
    stamp_snapshot = snapshot_target_file(target, STAMP_NAME, owner_only=False)
    current_stamp = stamp_snapshot.digest if stamp_snapshot is not None else None
    if current_stamp != desired_stamp:
        changes.append(STAMP_NAME)
    return {
        "schema_version": 1,
        "command": "plan",
        "target": str(target),
        "setup_id": setup_id,
        "operation": operation,
        "changes": changes,
        "backup_required": status["state"] == "managed",
        "mutates": False,
    }


def manager_result_still_current(
    target: Path,
    name: str,
    manager_result: FileSnapshot | None,
) -> bool:
    guard = current_target_guard(target)
    if guard is None or guard.target_fd is None:
        fail("selective rollback requires an anchored target directory")
    revalidate_guard_parent(guard)
    revalidate_held_target(guard)
    try:
        current_info = os.stat(
            name,
            dir_fd=guard.target_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        current_info = None
    if manager_result is None:
        return current_info is None
    if current_info is None or identity_of(current_info) != manager_result.identity:
        return False
    try:
        current = snapshot_held_target_file(guard, name, owner_only=False)
    except CodexSetupError:
        return False
    return current == manager_result


def selective_rollback(
    target: Path,
    desired: dict[str, bytes | None],
) -> tuple[str, ...]:
    guard = current_target_guard(target)
    if guard is None:
        fail("rollback requires an active anchored target lock")
    selected: list[str] = []
    expected: dict[str, FileSnapshot | None] = {name: None for name in (*MANAGED_FILES, STAMP_NAME)}
    for name in (*MANAGED_FILES, STAMP_NAME):
        if name not in guard.manager_results:
            continue
        manager_result = guard.manager_results[name]
        if manager_result_still_current(target, name, manager_result):
            selected.append(name)
            expected[name] = manager_result

    if selected:
        replace_managed_state(
            target,
            desired,
            expected,
            names=tuple(selected),
            allow_detached_target=True,
        )
    guard.mutated_paths.clear()
    guard.manager_results.clear()
    try:
        remove_created_target_if_empty(target)
    except ConcurrentTargetChange:
        # Preserve a concurrent replacement instead of deleting non-manager state.
        pass
    return tuple(selected)


def cleanup_unbacked_mutation(target: Path) -> None:
    empty: dict[str, bytes | None] = {name: None for name in (*MANAGED_FILES, STAMP_NAME)}
    selective_rollback(target, empty)


def verify_rollback_postcondition(
    target: Path,
    prior_status: dict[str, Any],
) -> None:
    if prior_status["state"] == "managed":
        restored = require_clean_managed(target)
        if restored["setup_id"] != prior_status["setup_id"]:
            fail("rollback restored the wrong setup identity")
        return

    for name in (*MANAGED_FILES, STAMP_NAME):
        assert_target_snapshot(target, name, None)
    if prior_status["state"] == "missing":
        remove_created_target_if_empty(target)
        if ensure_target_directory(target, create=False):
            fail("rollback could not restore the originally missing target")
    elif not ensure_target_directory(target, create=False):
        fail("rollback removed an originally existing unmanaged target")


def mutate_setup(target: Path, setup_id: str, command: str) -> dict[str, Any]:
    _, rendered = render_setup(setup_id)
    plan = plan_setup(target, setup_id)
    if command == "apply" and plan["operation"] == "switch":
        fail("apply cannot change setup identity; use switch")
    if command == "switch" and plan["operation"] != "switch":
        fail("switch requires a managed target with a different setup")
    if not plan["changes"]:
        return {
            "schema_version": 1,
            "command": command,
            "target": str(target),
            "setup_id": setup_id,
            "changed": [],
            "backup_slot": None,
        }

    with target_lock(target) as guard:
        prior_status = inspect_target(target)
        plan = plan_setup(target, setup_id)
        if command == "apply" and plan["operation"] == "switch":
            fail("apply cannot change setup identity; use switch")
        if command == "switch" and plan["operation"] != "switch":
            fail("switch requires a managed target with a different setup")
        before = capture_managed_snapshot(target)
        guard.expected_managed = before
        guard.mutated_paths.clear()
        guard.manager_results.clear()
        backup_slot: int | None = None
        rollback_desired: dict[str, bytes | None] | None = None
        backup_lease: BackupPoolLease | None = None
        if plan["backup_required"]:
            backup_slot, rollback_desired, backup_lease = create_transaction_backup(target)
        try:
            if backup_lease is not None:
                validate_backup_before_target_mutation(backup_lease)
                assert_managed_snapshot(target, before)
            # On update/switch the target is already clean-managed, so its config
            # tail holds only approved co-owned overlays (Codex project trust, the
            # builder enable, operator [mcp_servers.*]). Carry that tail onto the
            # new base instead of discarding it. A fresh install has no tail.
            config_payload: bytes | None = None
            if plan["operation"] in {"update", "switch"}:
                current_config, _ = read_target_file(
                    target,
                    "config.toml",
                    f"managed path {target / 'config.toml'}",
                    owner_only=False,
                    max_bytes=MANAGED_PAYLOAD_MAX_BYTES,
                )
                config_payload = preserve_config_overlays(current_config, rendered["config.toml"])
            apply_rendered(target, setup_id, rendered, config_payload=config_payload)
            final = require_clean_managed(target)
            reject_instruction_override(target, command)
            if final["setup_id"] != setup_id:
                fail("postcondition failed: setup identity mismatch")
            if backup_lease is not None:
                revalidate_backup_pool_lease(backup_lease)
        except BaseException as operation_error:
            try:
                if backup_slot is not None:
                    if rollback_desired is None:
                        fail("transaction backup was not loaded before mutation")
                    selective_rollback(target, rollback_desired)
                else:
                    cleanup_unbacked_mutation(target)
                if not isinstance(operation_error, ConcurrentTargetChange):
                    verify_rollback_postcondition(target, prior_status)
            except BaseException as rollback_error:
                raise CodexSetupError(
                    "setup mutation failed and rollback also failed: "
                    f"{type(operation_error).__name__}: {operation_error}"
                ) from rollback_error
            raise
        finally:
            if backup_lease is not None:
                close_backup_pool_lease(backup_lease)
    return {
        "schema_version": 1,
        "command": command,
        "target": str(target),
        "setup_id": setup_id,
        "changed": plan["changes"],
        "backup_slot": backup_slot,
    }


def restore_slot(target: Path, slot: int) -> dict[str, Any]:
    if not 0 <= slot <= 9:
        fail("--backup must be an integer from 0 to 9")
    status = inspect_target(target)
    if status["agents_override_present"]:
        reject_instruction_override(target, "restore")
    if status["state"] == "unmanaged" and status["unmanaged_managed_paths"]:
        fail("cannot restore over unmanaged managed paths")
    if status["state"] == "managed" and status["drift"]:
        fail(f"managed target has drift: {', '.join(status['drift'])}")
    with target_lock(target) as guard:
        current = inspect_target(target)
        if current["agents_override_present"]:
            reject_instruction_override(target, "restore")
        if current["state"] == "unmanaged" and current["unmanaged_managed_paths"]:
            fail("cannot restore over unmanaged managed paths")
        if current["state"] == "managed" and current["drift"]:
            fail(f"managed target has drift: {', '.join(current['drift'])}")
        backup_lease = open_backup_pool_lease(target)
        try:
            revalidate_backup_pool_lease(backup_lease)
            envelope, restore_desired, _ = load_backup_from_pool_fd(
                target,
                backup_lease.fd,
                slot,
            )
            if envelope["source_setup_id"] is None:
                fail("selected backup does not contain a managed Codex setup")
            before = capture_managed_snapshot(target)
            guard.expected_managed = before
            guard.mutated_paths.clear()
            guard.manager_results.clear()
            rollback_slot, rollback_desired, _ = create_transaction_backup(
                target,
                exclude=slot,
                lease=backup_lease,
            )
            validate_backup_before_target_mutation(backup_lease)
            assert_managed_snapshot(target, before)
            try:
                ensure_target_directory(target, create=True)
                replace_managed_state(target, restore_desired, before)
                reject_instruction_override(target, "restore")
                final = require_clean_managed(target)
                reject_instruction_override(target, "restore")
                if final["setup_id"] != envelope["source_setup_id"]:
                    fail("postcondition failed: restored setup identity mismatch")
                revalidate_backup_pool_lease(backup_lease)
            except BaseException as operation_error:
                try:
                    selective_rollback(target, rollback_desired)
                    if not isinstance(operation_error, ConcurrentTargetChange):
                        verify_rollback_postcondition(target, current)
                except BaseException as rollback_error:
                    raise CodexSetupError(
                        "restore failed and rollback also failed: "
                        f"{type(operation_error).__name__}: {operation_error}"
                    ) from rollback_error
                raise
        finally:
            close_backup_pool_lease(backup_lease)
    return {
        "schema_version": 1,
        "command": "restore",
        "target": str(target),
        "setup_id": envelope["source_setup_id"],
        "restored_backup_slot": slot,
        "rollback_backup_slot": rollback_slot,
    }


def remove_setup(target: Path) -> dict[str, Any]:
    require_clean_managed(target)
    with target_lock(target) as guard:
        status = require_clean_managed(target)
        before = capture_managed_snapshot(target)
        guard.expected_managed = before
        guard.mutated_paths.clear()
        guard.manager_results.clear()
        backup_slot, rollback_desired, backup_lease = create_transaction_backup(target)
        try:
            validate_backup_before_target_mutation(backup_lease)
            assert_managed_snapshot(target, before)
            desired: dict[str, bytes | None] = {name: None for name in (*MANAGED_FILES, STAMP_NAME)}
            replace_managed_state(target, desired, before)
            revalidate_backup_pool_lease(backup_lease)
        except BaseException as operation_error:
            try:
                selective_rollback(target, rollback_desired)
                if not isinstance(operation_error, ConcurrentTargetChange):
                    verify_rollback_postcondition(target, status)
            except BaseException as rollback_error:
                raise CodexSetupError(
                    "remove failed and rollback also failed: "
                    f"{type(operation_error).__name__}: {operation_error}"
                ) from rollback_error
            raise
        finally:
            close_backup_pool_lease(backup_lease)
    return {
        "schema_version": 1,
        "command": "remove",
        "target": str(target),
        "removed_setup_id": status["setup_id"],
        "backup_slot": backup_slot,
    }


def host_package_target() -> str:
    if not hasattr(os, "uname"):
        fail("standalone Codex installation is supported only on macOS and Linux")
    machine = os.uname().machine.lower()
    if machine in {"arm64", "aarch64"}:
        architecture = "aarch64"
    elif machine in {"x86_64", "amd64"}:
        architecture = "x86_64"
    else:
        fail(f"unsupported Codex installation architecture: {machine}")

    if sys.platform == "darwin":
        if architecture == "x86_64":
            try:
                translated = subprocess.run(
                    ["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={"PATH": CONTROLLED_INSTALLER_PATH, "LC_ALL": "C"},
                )
            except (OSError, subprocess.SubprocessError):
                translated = None
            if translated is not None and translated.returncode == 0:
                if translated.stdout.strip() == "1":
                    architecture = "aarch64"
        return f"{architecture}-apple-darwin"
    if sys.platform.startswith("linux"):
        return f"{architecture}-unknown-linux-musl"
    fail("standalone Codex installation is supported only on macOS and Linux")


def path_entry_exists(path: Path) -> bool:
    return os.path.lexists(path)


def validate_software_directory(path: Path, label: str) -> None:
    info = require_directory(path, label)
    if hasattr(os, "geteuid") and owner_of(info) != os.geteuid():
        fail(f"{label} must be owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o022:
        fail(f"{label} must not be writable by group or others")


def validate_software_executable(path: Path, label: str) -> None:
    info = require_regular_file(path, label)
    if hasattr(os, "geteuid") and owner_of(info) != os.geteuid():
        fail(f"{label} must be owned by the current user")
    mode = stat.S_IMODE(info.st_mode)
    if not mode & stat.S_IXUSR:
        fail(f"{label} must be executable by its owner")
    if mode & 0o022:
        fail(f"{label} must not be writable by group or others")


def resolve_software_symlink(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if not stat.S_ISLNK(info.st_mode):
        fail(f"{label} must be a symlink")
    if hasattr(os, "geteuid") and owner_of(info) != os.geteuid():
        fail(f"{label} must be owned by the current user")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        fail(f"{label} cannot be resolved: {exc}")


def read_package_metadata(path: Path) -> dict[str, Any]:
    content, info = read_regular_file(
        path,
        "Codex standalone package metadata",
        max_bytes=PACKAGE_METADATA_MAX_BYTES,
    )
    if hasattr(os, "geteuid") and owner_of(info) != os.geteuid():
        fail("Codex standalone package metadata must be owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o022:
        fail("Codex standalone package metadata must not be writable by group or others")
    metadata = parse_json_object(content, "Codex standalone package metadata")
    require_exact_keys(
        metadata,
        PACKAGE_METADATA_KEYS,
        "Codex standalone package metadata",
    )
    if metadata["layoutVersion"] != 1:
        fail("Codex standalone package layout version is unsupported")
    if metadata["variant"] != "codex":
        fail("Codex standalone package variant is not codex")
    if metadata["entrypoint"] != "bin/codex":
        fail("Codex standalone package entrypoint is invalid")
    if metadata["resourcesDir"] != "codex-resources":
        fail("Codex standalone package resources directory is invalid")
    if metadata["pathDir"] != "codex-path":
        fail("Codex standalone package path directory is invalid")
    version = metadata["version"]
    if not isinstance(version, str) or not SEMVER_PATTERN.fullmatch(version):
        fail("Codex standalone package version is invalid")
    expected_target = host_package_target()
    if metadata["target"] != expected_target:
        fail(
            "Codex standalone package target mismatch: "
            f"expected {expected_target}, got {metadata['target']!r}"
        )
    return metadata


def is_expected_temporary_codex_home_warning(diagnostics: str, target: Path) -> bool:
    try:
        canonical_target = target.resolve(strict=True)
    except OSError:
        return False
    temporary_root = Path(tempfile.gettempdir())
    if not temporary_root.is_absolute():
        return False
    try:
        canonical_target.relative_to(temporary_root)
    except ValueError:
        return False
    expected = (
        "WARNING: proceeding, even though we could not create PATH aliases: "
        "Refusing to create helper binaries under temporary dir "
        f"{json.dumps(str(temporary_root), ensure_ascii=False)} "
        "(codex_home: "
        f"AbsolutePathBuf({json.dumps(str(canonical_target), ensure_ascii=False)}))"
    )
    return diagnostics == expected


def bounded_codex_version(executable: Path, target: Path) -> str:
    environment = {
        "CODEX_HOME": str(target),
        "HOME": str(target),
        "USERPROFILE": str(target),
        "PATH": CONTROLLED_INSTALLER_PATH,
        "LANG": "C",
        "LC_ALL": "C",
    }
    with (
        tempfile.TemporaryFile() as stdout,
        tempfile.TemporaryFile() as stderr,
    ):
        try:
            completed = subprocess.run(
                [str(executable), "--version"],
                env=environment,
                cwd=target,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                check=False,
                timeout=VERSION_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            fail("installed Codex version check timed out")
        if completed.returncode != 0:
            fail(f"installed Codex version check failed with exit {completed.returncode}")
        for stream, label in ((stdout, "output"), (stderr, "diagnostics")):
            if stream.tell() > VERSION_OUTPUT_MAX_BYTES:
                fail(f"installed Codex version {label} exceeded its size limit")
        stdout.seek(0)
        stderr.seek(0)
        try:
            text = stdout.read().decode("utf-8").strip()
            diagnostics = stderr.read().decode("utf-8").strip()
        except UnicodeDecodeError:
            fail("installed Codex version output or diagnostics are not valid UTF-8")
    if diagnostics and not is_expected_temporary_codex_home_warning(diagnostics, target):
        fail("installed Codex returned unexpected version diagnostics")
    match = re.fullmatch(r"codex-cli ([0-9][0-9A-Za-z.+-]*)", text)
    if match is None or not SEMVER_PATTERN.fullmatch(match.group(1)):
        fail(f"installed Codex returned an invalid version string: {text!r}")
    return match.group(1)


def inspect_software_installation(target: Path) -> SoftwareInstallation | None:
    packages = target / "packages"
    standalone = packages / "standalone"
    releases = standalone / "releases"
    current = standalone / "current"
    visible_bin = target / "bin"
    visible = visible_bin / "codex"
    visible_host = visible_bin / "codex-code-mode-host"
    required_entries = (packages, standalone, releases, current, visible_bin, visible)
    if sys.platform == "darwin":
        required_entries = (*required_entries, visible_host)
    present = [path_entry_exists(path) for path in required_entries]
    if not any(present):
        return None
    if not all(present):
        fail("Codex standalone installation is incomplete")

    validate_software_directory(packages, "Codex packages directory")
    validate_software_directory(standalone, "Codex standalone root")
    validate_software_directory(releases, "Codex standalone releases directory")
    validate_software_directory(visible_bin, "Codex visible command directory")
    try:
        release_directory = resolve_software_symlink(current, "Codex standalone current entry")
        releases_resolved = releases.resolve(strict=True)
    except OSError as exc:
        fail(f"Codex standalone current release cannot be resolved: {exc}")
    if release_directory.parent != releases_resolved:
        fail("Codex standalone current release escapes the releases directory")
    validate_software_directory(release_directory, "Codex standalone current release")

    metadata = read_package_metadata(release_directory / "codex-package.json")
    expected_release_name = f"{metadata['version']}-{metadata['target']}"
    if release_directory.name != expected_release_name:
        fail("Codex standalone release directory identity is invalid")
    validate_software_directory(release_directory / "bin", "Codex standalone binary directory")
    validate_software_directory(
        release_directory / metadata["resourcesDir"],
        "Codex standalone resources directory",
    )
    validate_software_directory(
        release_directory / metadata["pathDir"],
        "Codex standalone path directory",
    )
    executable = release_directory / "bin" / "codex"
    validate_software_executable(executable, "Codex standalone executable")
    host_executable = release_directory / "bin" / "codex-code-mode-host"
    validate_software_executable(host_executable, "Codex standalone code-mode host")
    validate_software_executable(
        release_directory / metadata["pathDir"] / "rg",
        "Codex standalone ripgrep executable",
    )
    if sys.platform.startswith("linux"):
        validate_software_executable(
            release_directory / metadata["resourcesDir"] / "bwrap",
            "Codex standalone Linux sandbox executable",
        )
    compatibility_entrypoint = resolve_software_symlink(
        release_directory / "codex", "Codex standalone compatibility entrypoint"
    )
    if compatibility_entrypoint != executable:
        fail("Codex standalone compatibility entrypoint is not bound to bin/codex")

    visible_target = resolve_software_symlink(visible, "visible Codex command")
    if visible_target != executable:
        fail("visible Codex command is not bound to the current standalone release")
    if sys.platform == "darwin":
        visible_host_target = resolve_software_symlink(visible_host, "visible Codex code-mode host")
        if visible_host_target != host_executable:
            fail("visible Codex code-mode host is not bound to the current standalone release")
    version = bounded_codex_version(executable, target)
    if version != metadata["version"]:
        fail("Codex standalone package metadata and executable versions disagree")
    return SoftwareInstallation(
        version=version,
        executable=executable,
        release_directory=release_directory,
        host_target=metadata["target"],
    )


def require_current_software(target: Path) -> SoftwareInstallation:
    installation = inspect_software_installation(target)
    if installation is None:
        fail("Codex CLI is not installed at the selected target; run install-cli")
    if installation.version != TESTED_CODEX_VERSION:
        fail(
            f"Codex CLI {installation.version} is not current; "
            f"run update-cli to install {TESTED_CODEX_VERSION}"
        )
    return installation


def software_status(target: Path) -> dict[str, Any]:
    installation = inspect_software_installation(target)
    return {
        "schema_version": 1,
        "command": "software-status",
        "target": str(target),
        "installed": installation is not None,
        "current": (installation is not None and installation.version == TESTED_CODEX_VERSION),
        "version": installation.version if installation is not None else None,
        "executable": str(installation.executable) if installation is not None else None,
    }


def builder_source_contract() -> tuple[str, bytes]:
    version_document = load_json_object(
        ROOT / "build" / "version.json",
        "build version metadata",
    )
    plugin_version = version_document.get("nddev_builder_plugin_version")
    if not isinstance(plugin_version, str) or not SEMVER_PATTERN.fullmatch(plugin_version):
        fail("nddev-builder source version is invalid")
    if version_document.get("build_version") != VERSION:
        fail("public build version metadata is not synchronized")

    plugin_path = ROOT / "plugins" / BUILDER_PLUGIN_ID / ".codex-plugin" / "plugin.json"
    plugin_content, _ = read_regular_file(
        plugin_path,
        "nddev-builder source plugin manifest",
        max_bytes=METADATA_MAX_BYTES,
    )
    plugin = parse_json_object(plugin_content, "nddev-builder source plugin manifest")
    if plugin.get("name") != BUILDER_PLUGIN_ID or plugin.get("version") != plugin_version:
        fail("nddev-builder source plugin identity or version is invalid")

    marketplace = load_json_object(
        ROOT / ".agents" / "plugins" / "marketplace.json",
        "nddev-builder source marketplace manifest",
    )
    plugins = marketplace.get("plugins")
    if marketplace.get("name") != BUILDER_MARKETPLACE_ID or not isinstance(plugins, list):
        fail("nddev-builder source marketplace identity is invalid")
    matching = [
        entry
        for entry in plugins
        if isinstance(entry, dict) and entry.get("name") == BUILDER_PLUGIN_ID
    ]
    if len(matching) != 1:
        fail("nddev-builder source marketplace must contain exactly one builder plugin")
    source = matching[0].get("source")
    if source != {"source": "local", "path": "./plugins/nddev-builder"}:
        fail("nddev-builder source marketplace plugin path is invalid")
    return plugin_version, plugin_content


def builder_profile_bytes() -> bytes:
    source = json.dumps(str(ROOT), ensure_ascii=False)
    return (
        f"[marketplaces.{BUILDER_MARKETPLACE_ID}]\n"
        'source_type = "local"\n'
        f"source = {source}\n"
        "\n"
        f'[plugins."{BUILDER_PLUGIN_QUALIFIED_ID}"]\n'
        "enabled = true\n"
    ).encode("utf-8")


def builder_config_enabled(config: bytes, block: bytes) -> bool:
    """Return True when the canonical builder block is present verbatim in the
    config.toml bytes. The block is written contiguously and the Codex runtime
    only appends after it, so a substring test is precise and tolerant of the
    runtime's later ``[projects.*]`` additions."""
    return block in config


def config_with_builder_block(config: bytes, block: bytes) -> bytes:
    """Return config guaranteed to contain the canonical builder block exactly
    once, appended as a co-owned addition after the managed setup base.
    Idempotent: if the block is already present the config is returned unchanged,
    so a repeated enable never duplicates it."""
    if builder_config_enabled(config, block):
        return config
    if not config.endswith(b"\n"):
        config += b"\n"
    return config + b"\n" + block


def _builder_enabled_on_disk(target: Path, block: bytes) -> bool:
    """Read the target's config.toml and report whether the canonical builder
    block is enabled in it. Any read failure is treated as not-enabled
    (conservative: it triggers a re-enable rather than masking a missing block)."""
    try:
        content, _ = read_target_file(
            target,
            "config.toml",
            f"managed path {target / 'config.toml'}",
            owner_only=False,
            max_bytes=MANAGED_PAYLOAD_MAX_BYTES,
        )
    except (CodexSetupError, OSError, ValueError):
        return False
    return builder_config_enabled(content, block)


def validate_builder_mutated_config(content: bytes, original: bytes) -> None:
    if not content.startswith(original) or not original.endswith(b"\n"):
        fail("official Codex plugin commands replaced the managed setup configuration")
    try:
        suffix = content[len(original) :].decode("utf-8")
    except UnicodeDecodeError:
        fail("official Codex plugin commands wrote invalid UTF-8 configuration")
    lines = suffix.splitlines()
    expected_prefix = [
        "",
        f"[marketplaces.{BUILDER_MARKETPLACE_ID}]",
    ]
    if lines[:2] != expected_prefix or len(lines) != 8 or not content.endswith(b"\n"):
        fail("official Codex plugin commands wrote an unexpected configuration shape")
    if (
        re.fullmatch(
            r'last_updated = "[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"',
            lines[2],
        )
        is None
    ):
        fail("official Codex plugin commands wrote an invalid marketplace timestamp")
    if lines[3] != 'source_type = "local"' or not lines[4].startswith("source = "):
        fail("official Codex plugin commands wrote an invalid marketplace source")
    try:
        source = json.loads(lines[4].removeprefix("source = "))
    except json.JSONDecodeError:
        fail("official Codex plugin commands wrote an invalid marketplace source string")
    if source != str(ROOT):
        fail("official Codex plugin commands bound the marketplace to the wrong source")
    if lines[5:] != [
        "",
        f'[plugins."{BUILDER_PLUGIN_QUALIFIED_ID}"]',
        "enabled = true",
    ]:
        fail("official Codex plugin commands enabled an unexpected plugin configuration")


def validate_builder_cache_directory_info(info: os.stat_result, label: str) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail(f"{label} must be a real directory")
    if hasattr(os, "geteuid") and owner_of(info) != os.geteuid():
        fail(f"{label} must be owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o022:
        fail(f"{label} must not be writable by group or others")


def validate_builder_cache_directory(path: Path, label: str) -> None:
    validate_builder_cache_directory_info(require_directory(path, label), label)


def read_builder_plugin_tree(
    root: Path,
    label: str,
) -> tuple[set[str], dict[str, bytes]]:
    validate_builder_cache_directory(root, label)
    directories: set[str] = set()
    files: dict[str, bytes] = {}
    total_bytes = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            fail(f"cannot inspect {label}: {exc}")
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if (
                entry.name == "__pycache__"
                or entry.name == ".DS_Store"
                or entry.name.endswith(".pyc")
            ):
                fail(f"{label} contains a forbidden runtime cache entry: {relative}")
            try:
                info = path.lstat()
            except OSError as exc:
                fail(f"cannot inspect {label} entry {relative}: {exc}")
            if stat.S_ISLNK(info.st_mode):
                fail(f"{label} contains a symlink: {relative}")
            if stat.S_ISDIR(info.st_mode):
                if len(directories) >= BUILDER_TREE_MAX_DIRECTORIES:
                    fail(f"{label} exceeds the {BUILDER_TREE_MAX_DIRECTORIES}-directory limit")
                validate_builder_cache_directory(path, f"{label} directory {relative}")
                directories.add(relative)
                pending.append(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                fail(f"{label} contains a non-regular entry: {relative}")
            if len(files) >= BUILDER_TREE_MAX_FILES:
                fail(f"{label} exceeds the {BUILDER_TREE_MAX_FILES}-file limit")
            content, final = read_regular_file(
                path,
                f"{label} file {relative}",
                max_bytes=BUILDER_TREE_MAX_BYTES,
            )
            if hasattr(os, "geteuid") and owner_of(final) != os.geteuid():
                fail(f"{label} file {relative} must be owned by the current user")
            if stat.S_IMODE(final.st_mode) & 0o022:
                fail(f"{label} file {relative} must not be writable by group or others")
            total_bytes += len(content)
            if total_bytes > BUILDER_TREE_MAX_BYTES:
                fail(f"{label} exceeds the {BUILDER_TREE_MAX_BYTES}-byte aggregate limit")
            files[relative] = content
    return directories, files


def inspect_builder_cache(target: Path, plugin_version: str, source_manifest: bytes) -> str:
    current = target
    for component, label in (
        ("plugins", "Codex plugin directory"),
        ("cache", "Codex plugin cache directory"),
        (BUILDER_MARKETPLACE_ID, "nddev-builder marketplace cache directory"),
        (BUILDER_PLUGIN_ID, "nddev-builder plugin cache directory"),
    ):
        current = current / component
        if not path_entry_exists(current):
            return "missing"
        validate_builder_cache_directory(current, label)

    version_root = current / plugin_version
    if not path_entry_exists(version_root):
        return "missing"
    validate_builder_cache_directory(version_root, "nddev-builder version cache directory")
    manifest_directory = version_root / ".codex-plugin"
    if not path_entry_exists(manifest_directory):
        return "drifted"
    validate_builder_cache_directory(
        manifest_directory,
        "nddev-builder cached manifest directory",
    )
    manifest_path = manifest_directory / "plugin.json"
    if not path_entry_exists(manifest_path):
        return "drifted"
    cached_content, info = read_regular_file(
        manifest_path,
        "nddev-builder cached plugin manifest",
        max_bytes=METADATA_MAX_BYTES,
    )
    if hasattr(os, "geteuid") and owner_of(info) != os.geteuid():
        fail("nddev-builder cached plugin manifest must be owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o022:
        fail("nddev-builder cached plugin manifest must not be writable by group or others")
    if cached_content != source_manifest:
        return "drifted"
    cached = parse_json_object(cached_content, "nddev-builder cached plugin manifest")
    if cached.get("name") != BUILDER_PLUGIN_ID or cached.get("version") != plugin_version:
        return "drifted"
    source_root = ROOT / "plugins" / BUILDER_PLUGIN_ID
    source_directories, source_files = read_builder_plugin_tree(
        source_root,
        "nddev-builder source plugin tree",
    )
    cached_directories, cached_files = read_builder_plugin_tree(
        version_root,
        "nddev-builder cached plugin tree",
    )
    if source_directories != cached_directories or source_files != cached_files:
        return "drifted"
    return "current"


def inspect_builder_profile(target: Path, expected: bytes) -> str:
    if target_entry_info(target, BUILDER_PROFILE_NAME) is None:
        return "missing"
    content, _ = read_target_file(
        target,
        BUILDER_PROFILE_NAME,
        f"builder profile {target / BUILDER_PROFILE_NAME}",
        owner_only=True,
        max_bytes=METADATA_MAX_BYTES,
    )
    return "current" if content == expected else "drifted"


def builder_status(target: Path) -> dict[str, Any]:
    plugin_version, source_manifest = builder_source_contract()
    expected_block = builder_profile_bytes()
    profile_state = "missing"
    cache_state = "missing"
    config_enabled = False
    if ensure_target_directory(target, create=False):
        profile_state = inspect_builder_profile(target, expected_block)
        cache_state = inspect_builder_cache(target, plugin_version, source_manifest)
        config_enabled = _builder_enabled_on_disk(target, expected_block)
    installation = inspect_software_installation(target)
    installed = config_enabled and profile_state == "current" and cache_state == "current"
    state = "installed" if installed else "missing"
    if not installed and (config_enabled or profile_state != "missing" or cache_state != "missing"):
        state = "incomplete"
    return {
        "schema_version": 1,
        "command": "builder-status",
        "target": str(target),
        "state": state,
        "installed": installed,
        "current": installed
        and installation is not None
        and installation.version == TESTED_CODEX_VERSION,
        "profile": BUILDER_PROFILE_NAME,
        "profile_state": profile_state,
        "cache_state": cache_state,
        "config_enabled": config_enabled,
        "plugin_version": plugin_version,
        "codex_version": installation.version if installation is not None else None,
    }


def download_verified_installer(destination: Path) -> None:
    if INSTALLER_SIZE_BYTES > INSTALLER_MAX_BYTES:
        fail("pinned Codex installer exceeds the download policy")
    request = urllib.request.Request(
        INSTALLER_URL,
        headers={"User-Agent": f"nddev-codex-app/{VERSION}"},
    )
    digest = hashlib.sha256()
    downloaded = 0
    try:
        with (
            urllib.request.urlopen(request, timeout=120) as response,
            destination.open("xb") as output,
        ):
            while True:
                block = response.read(DOWNLOAD_CHUNK_SIZE)
                if not block:
                    break
                downloaded += len(block)
                if downloaded > INSTALLER_SIZE_BYTES or downloaded > INSTALLER_MAX_BYTES:
                    fail("Codex installer exceeded its pinned size")
                output.write(block)
                digest.update(block)
            output.flush()
            os.fsync(output.fileno())
    except (OSError, urllib.error.URLError) as exc:
        fail(f"cannot download pinned Codex installer: {exc}")
    if downloaded != INSTALLER_SIZE_BYTES:
        fail(f"Codex installer size mismatch: expected {INSTALLER_SIZE_BYTES}, got {downloaded}")
    actual_digest = digest.hexdigest()
    if actual_digest != INSTALLER_SHA256:
        fail(f"Codex installer SHA-256 mismatch: expected {INSTALLER_SHA256}, got {actual_digest}")
    destination.chmod(0o700)
    validate_software_executable(destination, "verified Codex installer")


def installer_environment(target: Path, workspace: Path) -> dict[str, str]:
    temporary_home = workspace / "home"
    temporary_root = workspace / "tmp"
    temporary_home.mkdir(mode=OWNER_DIRECTORY_MODE)
    temporary_root.mkdir(mode=OWNER_DIRECTORY_MODE)
    tools_directory = write_installer_curl_wrapper(workspace)
    return {
        "CODEX_HOME": str(target),
        "CODEX_INSTALL_DIR": str(target / "bin"),
        "CODEX_NON_INTERACTIVE": "1",
        "CODEX_RELEASE": TESTED_CODEX_VERSION,
        "HOME": str(temporary_home),
        "USERPROFILE": str(temporary_home),
        "TMPDIR": str(temporary_root),
        "PATH": f"{tools_directory}:{CONTROLLED_INSTALLER_PATH}",
        "SHELL": "/bin/sh",
        "LANG": "C",
        "LC_ALL": "C",
    }


def trusted_system_curl() -> Path:
    for candidate in (Path("/usr/bin/curl"), Path("/bin/curl")):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            fail(f"cannot inspect system curl: {exc}")
        mode = stat.S_IMODE(info.st_mode)
        if (
            stat.S_ISREG(info.st_mode)
            and owner_of(info) == 0
            and mode & stat.S_IXUSR
            and not mode & 0o022
        ):
            return candidate
    fail("a root-owned, non-writable system curl is required to install Codex")


def write_installer_curl_wrapper(workspace: Path) -> Path:
    system_curl = trusted_system_curl()
    package_name, package_digest = PACKAGE_ASSETS[host_package_target()]
    metadata = canonical_json(
        {
            "tag_name": INSTALLER_RELEASE_TAG,
            "assets": [
                {
                    "name": package_name,
                    "digest": f"sha256:{package_digest}",
                },
                {
                    "name": PACKAGE_CHECKSUM_ASSET,
                    "digest": f"sha256:{PACKAGE_CHECKSUM_SHA256}",
                },
            ],
        }
    ).decode("utf-8")
    tools_directory = workspace / "tools"
    tools_directory.mkdir(mode=OWNER_DIRECTORY_MODE)
    validate_software_directory(tools_directory, "installer tools directory")
    wrapper = tools_directory / "curl"
    content = (
        "#!/bin/sh\n"
        "set -eu\n"
        'for argument in "$@"; do\n'
        f"  if [ \"$argument\" = '{RELEASE_METADATA_URL}' ]; then\n"
        "    cat <<'NDDEV_CODEX_RELEASE_METADATA'\n"
        f"{metadata}"
        "NDDEV_CODEX_RELEASE_METADATA\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        f"exec '{system_curl}' \"$@\"\n"
    ).encode("utf-8")
    write_new_file(wrapper, content, 0o700)
    validate_software_executable(wrapper, "installer curl wrapper")
    return tools_directory


def run_verified_installer(installer: Path, target: Path, workspace: Path) -> None:
    process: subprocess.Popen[bytes] | None = None
    captured = bytearray()
    try:
        environment = installer_environment(target, workspace)
        prior_umask = os.umask(0o077)
        try:
            process = subprocess.Popen(
                ["/bin/sh", str(installer)],
                cwd=workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            os.umask(prior_umask)
        if process.stdout is None:
            fail("official Codex installer output pipe is unavailable")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + INSTALLER_TIMEOUT_SECONDS
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    fail("official Codex installer timed out")
                events = selector.select(timeout=min(remaining, 0.5))
                if not events:
                    if process.poll() is not None:
                        block = os.read(process.stdout.fileno(), 65536)
                        if block:
                            captured.extend(block)
                            if len(captured) > INSTALLER_OUTPUT_MAX_BYTES:
                                fail("official Codex installer output exceeded its size limit")
                            continue
                        break
                    continue
                block = os.read(process.stdout.fileno(), 65536)
                if not block:
                    break
                captured.extend(block)
                if len(captured) > INSTALLER_OUTPUT_MAX_BYTES:
                    fail("official Codex installer output exceeded its size limit")
        finally:
            selector.close()
            process.stdout.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail("official Codex installer timed out")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            fail("official Codex installer timed out")
    except BaseException as exc:
        terminate_installer_process(process)
        if isinstance(exc, OSError):
            fail(f"cannot execute official Codex installer: {exc}")
        raise
    if len(captured) > INSTALLER_OUTPUT_MAX_BYTES:
        fail("official Codex installer output exceeded its size limit")
    if returncode != 0:
        terminate_installer_process(process)
        detail = captured.decode("utf-8", errors="replace")
        detail = re.sub(r"[^\x09\x0a\x20-\x7e]", "?", detail)[-2000:].strip()
        suffix = f"; output: {detail}" if detail else ""
        fail(f"official Codex installer failed with exit {returncode}{suffix}")


def terminate_installer_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=INSTALLER_KILL_WAIT_SECONDS)
        return
    except OSError:
        if process.poll() is None:
            process.kill()
    if wait_for_installer_process_group(
        process,
        process_group,
        INSTALLER_TERMINATION_GRACE_SECONDS,
    ):
        return
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        if process.poll() is None:
            process.kill()
    if not wait_for_installer_process_group(
        process,
        process_group,
        INSTALLER_KILL_WAIT_SECONDS,
    ):
        fail("official Codex installer process group could not be terminated")


def installer_process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_installer_process_group(
    process: subprocess.Popen[bytes],
    process_group: int,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        process.poll()
        if not installer_process_group_exists(process_group):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))


def run_builder_plugin_command(
    installation: SoftwareInstallation,
    target: Path,
    arguments: list[str],
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(target)
    process: subprocess.Popen[bytes] | None = None
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        process = subprocess.Popen(
            [str(installation.executable), *arguments],
            cwd=target,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            fail("official Codex plugin command output pipes are unavailable")
        selector = selectors.DefaultSelector()
        streams = {
            process.stdout.fileno(): (process.stdout, "stdout"),
            process.stderr.fileno(): (process.stderr, "stderr"),
        }
        for stream, _ in streams.values():
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + BUILDER_COMMAND_TIMEOUT_SECONDS
        try:
            while streams:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    fail("official Codex plugin command timed out")
                events = selector.select(timeout=min(remaining, 0.5))
                if not events:
                    continue
                for key, _ in events:
                    stream, label = streams[key.fd]
                    block = os.read(key.fd, 65536)
                    if not block:
                        selector.unregister(stream)
                        del streams[key.fd]
                        stream.close()
                        continue
                    captured[label].extend(block)
                    if (
                        sum(len(value) for value in captured.values())
                        > BUILDER_COMMAND_OUTPUT_MAX_BYTES
                    ):
                        fail("official Codex plugin command output exceeded its size limit")
        finally:
            selector.close()
            for stream, _ in streams.values():
                stream.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail("official Codex plugin command timed out")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            fail("official Codex plugin command timed out")
    except BaseException as exc:
        terminate_installer_process(process)
        if isinstance(exc, OSError):
            fail(f"cannot execute official Codex plugin command: {exc}")
        raise

    try:
        stdout = bytes(captured["stdout"]).decode("utf-8")
        stderr = bytes(captured["stderr"]).decode("utf-8")
    except UnicodeDecodeError:
        fail("official Codex plugin command output is not valid UTF-8")
    if returncode != 0:
        detail = re.sub(r"[^\x09\x0a\x20-\x7e]", "?", (stderr or stdout))[-2000:].strip()
        suffix = f"; output: {detail}" if detail else ""
        fail(f"official Codex plugin command failed with exit {returncode}{suffix}")
    diagnostics = stderr.strip()
    if diagnostics and not is_expected_temporary_codex_home_warning(diagnostics, target):
        fail("official Codex plugin command returned unexpected diagnostics")
    return parse_json_object(stdout.encode("utf-8"), "official Codex plugin command output")


def validate_builder_marketplace_result(result: dict[str, Any]) -> None:
    require_exact_keys(
        result,
        {"marketplaceName", "installedRoot", "alreadyAdded"},
        "official Codex marketplace-add result",
    )
    if result["marketplaceName"] != BUILDER_MARKETPLACE_ID:
        fail("official Codex marketplace-add result has the wrong marketplace identity")
    if not isinstance(result["alreadyAdded"], bool):
        fail("official Codex marketplace-add result has an invalid alreadyAdded flag")
    installed_root = result["installedRoot"]
    if not isinstance(installed_root, str) or not Path(installed_root).is_absolute():
        fail("official Codex marketplace-add result has an invalid installed root")
    try:
        resolved_root = Path(installed_root).resolve(strict=True)
    except OSError as exc:
        fail(f"official Codex marketplace-add installed root cannot be resolved: {exc}")
    if resolved_root != ROOT.resolve(strict=True):
        fail("official Codex marketplace-add result is bound to the wrong source root")


def builder_cache_root(target: Path, plugin_version: str) -> Path:
    return (
        target / "plugins" / "cache" / BUILDER_MARKETPLACE_ID / BUILDER_PLUGIN_ID / plugin_version
    )


def builder_cache_ancestor_paths(target: Path) -> tuple[Path, ...]:
    plugin_directory = target / "plugins"
    cache_directory = plugin_directory / "cache"
    marketplace_directory = cache_directory / BUILDER_MARKETPLACE_ID
    return (
        plugin_directory,
        cache_directory,
        marketplace_directory,
        marketplace_directory / BUILDER_PLUGIN_ID,
    )


def capture_builder_cache_tree(
    root: Path,
    label: str,
) -> BuilderCacheTreeSnapshot | None:
    if not path_entry_exists(root):
        return None
    before = require_directory(root, label)
    validate_builder_cache_directory_info(before, label)
    root_identity = identity_of(before)
    root_mode = stat.S_IMODE(before.st_mode)
    root_fd = open_directory_fd(root)
    directory_modes: dict[str, int] = {}
    files: dict[str, tuple[bytes, int]] = {}
    directory_count = 0
    file_count = 0
    total_bytes = 0

    def capture_directory(directory_fd: int, prefix: str) -> None:
        nonlocal directory_count, file_count, total_bytes
        for name in sorted(os.listdir(directory_fd)):
            relative = f"{prefix}/{name}" if prefix else name
            if name == "__pycache__" or name == ".DS_Store" or name.endswith(".pyc"):
                fail(f"{label} contains a forbidden runtime cache entry: {relative}")
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                fail_concurrent(f"{label} entry disappeared during snapshot: {relative}")
            if stat.S_ISLNK(info.st_mode):
                fail(f"{label} contains a symlink: {relative}")
            if stat.S_ISDIR(info.st_mode):
                directory_count += 1
                if directory_count > BUILDER_TREE_MAX_DIRECTORIES:
                    fail(f"{label} exceeds the {BUILDER_TREE_MAX_DIRECTORIES}-directory limit")
                validate_builder_cache_directory_info(
                    info,
                    f"{label} directory {relative}",
                )
                child_fd = open_directory_fd(name, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if identity_of(opened) != identity_of(info):
                        fail_concurrent(f"{label} directory changed while opening: {relative}")
                    validate_builder_cache_directory_info(
                        opened,
                        f"{label} directory {relative}",
                    )
                    directory_modes[relative] = stat.S_IMODE(opened.st_mode)
                    capture_directory(child_fd, relative)
                    final = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if identity_of(final) != identity_of(opened) or stat.S_IMODE(
                        final.st_mode
                    ) != stat.S_IMODE(opened.st_mode):
                        fail_concurrent(f"{label} directory changed during snapshot: {relative}")
                    validate_builder_cache_directory_info(
                        final,
                        f"{label} directory {relative}",
                    )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(info.st_mode):
                fail(f"{label} contains a non-regular entry: {relative}")
            file_count += 1
            if file_count > BUILDER_TREE_MAX_FILES:
                fail(f"{label} exceeds the {BUILDER_TREE_MAX_FILES}-file limit")
            content, final = read_file_at(
                directory_fd,
                name,
                f"{label} file {relative}",
                max_bytes=BUILDER_TREE_MAX_BYTES,
            )
            if hasattr(os, "geteuid") and owner_of(final) != os.geteuid():
                fail(f"{label} file {relative} must be owned by the current user")
            mode = stat.S_IMODE(final.st_mode)
            if mode & 0o022:
                fail(f"{label} file {relative} must not be writable by group or others")
            total_bytes += len(content)
            if total_bytes > BUILDER_TREE_MAX_BYTES:
                fail(f"{label} exceeds the {BUILDER_TREE_MAX_BYTES}-byte aggregate limit")
            files[relative] = (content, mode)

    try:
        opened = os.fstat(root_fd)
        if identity_of(opened) != root_identity:
            fail_concurrent(f"{label} changed while it was being opened")
        validate_builder_cache_directory_info(opened, label)
        capture_directory(root_fd, "")
        final = require_directory(root, label)
        if (
            identity_of(final) != root_identity
            or identity_of(os.fstat(root_fd)) != root_identity
            or stat.S_IMODE(final.st_mode) != root_mode
        ):
            fail_concurrent(f"{label} changed during snapshot")
        validate_builder_cache_directory_info(final, label)
    finally:
        os.close(root_fd)
    return BuilderCacheTreeSnapshot(
        root_identity=root_identity,
        root_mode=root_mode,
        directory_modes=directory_modes,
        files=files,
    )


def capture_builder_cache_transaction(
    target: Path,
    plugin_version: str,
) -> BuilderCacheTransactionSnapshot:
    guard = current_target_guard(target)
    if guard is not None:
        revalidate_guard(guard, allow_missing=False)
    ancestors: list[BuilderCacheDirectorySnapshot | None] = []
    for index, path in enumerate(builder_cache_ancestor_paths(target)):
        if not path_entry_exists(path):
            ancestors.append(None)
            continue
        label = f"nddev-builder cache ancestor {index}"
        info = require_directory(path, label)
        validate_builder_cache_directory_info(info, label)
        ancestors.append(
            BuilderCacheDirectorySnapshot(
                identity=identity_of(info),
                mode=stat.S_IMODE(info.st_mode),
            )
        )
    tree = capture_builder_cache_tree(
        builder_cache_root(target, plugin_version),
        "nddev-builder version cache transaction snapshot",
    )
    if guard is not None:
        revalidate_guard(guard, allow_missing=False)
    return BuilderCacheTransactionSnapshot(tuple(ancestors), tree)


def delete_untrusted_builder_cache_directory_contents(
    directory_fd: int,
    label: str,
    root_device: int,
) -> None:
    for name in sorted(os.listdir(directory_fd)):
        relative_label = f"{label} entry {name}"
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            fail_concurrent(f"{relative_label} disappeared during rollback")
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            if info.st_dev != root_device:
                fail(f"{relative_label} crosses a filesystem boundary")
            child_fd = open_directory_fd(name, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if identity_of(opened) != identity_of(info):
                    fail_concurrent(f"{relative_label} changed while opening")
                if opened.st_dev != root_device:
                    fail(f"{relative_label} crosses a filesystem boundary")
                os.fchmod(child_fd, OWNER_DIRECTORY_MODE)
                delete_untrusted_builder_cache_directory_contents(
                    child_fd,
                    relative_label,
                    root_device,
                )
                final = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    identity_of(final) != identity_of(opened)
                    or identity_of(os.fstat(child_fd)) != identity_of(opened)
                    or final.st_dev != root_device
                ):
                    fail_concurrent(f"{relative_label} changed during rollback")
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
            continue
        # Unlink every non-directory entry by its anchored name. Symlinks,
        # hard links, sockets, FIFOs, and invalid regular files are never opened
        # or followed outside the quarantined version-cache directory.
        os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def remove_builder_cache_tree(root: Path, label: str) -> None:
    if not path_entry_exists(root):
        return
    parent = root.parent
    validate_builder_cache_directory(parent, f"{label} parent")
    parent_fd = open_directory_fd(parent)
    quarantine_name = f".nddev-builder-rollback-{secrets.token_hex(8)}"
    moved = False
    mutation_started = False
    try:
        current = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        root_identity = identity_of(current)
        anchored_rename(
            root.name,
            quarantine_name,
            source_fd=parent_fd,
            destination_fd=parent_fd,
        )
        moved = True
        quarantined = os.stat(quarantine_name, dir_fd=parent_fd, follow_symlinks=False)
        if identity_of(quarantined) != root_identity:
            fail_concurrent(f"{label} changed during rollback quarantine")
        if entry_exists_at(parent_fd, root.name):
            fail_concurrent(f"{label} reappeared during rollback")
        mutation_started = True
        if stat.S_ISDIR(quarantined.st_mode) and not stat.S_ISLNK(quarantined.st_mode):
            quarantine_fd = open_directory_fd(quarantine_name, dir_fd=parent_fd)
            try:
                opened = os.fstat(quarantine_fd)
                if identity_of(opened) != root_identity:
                    fail_concurrent(f"{label} quarantine handle changed")
                os.fchmod(quarantine_fd, OWNER_DIRECTORY_MODE)
                delete_untrusted_builder_cache_directory_contents(
                    quarantine_fd,
                    f"{label} rollback quarantine",
                    opened.st_dev,
                )
                final = os.stat(
                    quarantine_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    identity_of(final) != root_identity
                    or identity_of(os.fstat(quarantine_fd)) != root_identity
                ):
                    fail_concurrent(f"{label} quarantine changed before removal")
            finally:
                os.close(quarantine_fd)
            os.rmdir(quarantine_name, dir_fd=parent_fd)
        else:
            os.unlink(quarantine_name, dir_fd=parent_fd)
        moved = False
        os.fsync(parent_fd)
    except BaseException as operation_error:
        recovery_error: BaseException | None = None
        if moved and not mutation_started:
            try:
                if not entry_exists_at(parent_fd, root.name):
                    anchored_rename(
                        quarantine_name,
                        root.name,
                        source_fd=parent_fd,
                        destination_fd=parent_fd,
                    )
            except BaseException as exc:
                recovery_error = exc
        if recovery_error is not None:
            raise CodexSetupError(
                f"{label} removal failed and rollback quarantine restoration also failed: "
                f"{type(operation_error).__name__}: {operation_error}; "
                f"quarantined entry preserved at {parent / quarantine_name}"
            ) from recovery_error
        raise
    finally:
        os.close(parent_fd)


def create_builder_cache_tree(
    root: Path,
    snapshot: BuilderCacheTreeSnapshot,
) -> None:
    parent = root.parent
    validate_builder_cache_directory(parent, "nddev-builder cache restore parent")
    parent_fd = open_directory_fd(parent)
    stage_name = f".nddev-builder-restore-{secrets.token_hex(8)}"
    stage = parent / stage_name
    created = False
    try:
        if entry_exists_at(parent_fd, root.name):
            fail_concurrent("nddev-builder cache reappeared before restoration")
        os.mkdir(stage_name, OWNER_DIRECTORY_MODE, dir_fd=parent_fd)
        created = True
        stage_fd = open_directory_fd(stage_name, dir_fd=parent_fd)
        try:
            for relative, _mode in sorted(
                snapshot.directory_modes.items(),
                key=lambda item: (item[0].count("/"), item[0]),
            ):
                path = stage / relative
                path.mkdir(mode=OWNER_DIRECTORY_MODE)
            for relative, (content, mode) in sorted(snapshot.files.items()):
                write_new_file(stage / relative, content, mode)
            for relative, mode in sorted(
                snapshot.directory_modes.items(),
                key=lambda item: (-item[0].count("/"), item[0]),
            ):
                directory_fd = open_directory_fd(stage / relative)
                try:
                    os.fchmod(directory_fd, mode)
                    validate_builder_cache_directory_info(
                        os.fstat(directory_fd),
                        f"restored nddev-builder cache directory {relative}",
                    )
                finally:
                    os.close(directory_fd)
            os.fchmod(stage_fd, snapshot.root_mode)
            validate_builder_cache_directory_info(
                os.fstat(stage_fd),
                "restored nddev-builder cache root",
            )
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        if (
            capture_builder_cache_tree(
                stage,
                "staged nddev-builder cache restoration",
            )
            != snapshot
        ):
            fail("staged nddev-builder cache restoration does not match its snapshot")
        if entry_exists_at(parent_fd, root.name):
            fail_concurrent("nddev-builder cache reappeared during restoration")
        anchored_rename(
            stage_name,
            root.name,
            source_fd=parent_fd,
            destination_fd=parent_fd,
        )
        created = False
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
        if created and path_entry_exists(stage):
            remove_builder_cache_tree(stage, "failed nddev-builder cache restoration")


def restore_builder_cache_transaction(
    target: Path,
    plugin_version: str,
    expected: BuilderCacheTransactionSnapshot,
) -> None:
    guard = current_target_guard(target)
    if guard is not None:
        revalidate_guard(guard, allow_missing=False)
    ancestor_paths = builder_cache_ancestor_paths(target)
    for index, (path, prior) in enumerate(zip(ancestor_paths, expected.ancestors, strict=True)):
        if not path_entry_exists(path):
            continue
        label = f"nddev-builder cache ancestor {index}"
        info = require_directory(path, label)
        if prior is not None and identity_of(info) != prior.identity:
            fail_concurrent(f"{label} changed during install-builder")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            fail(f"{label} must remain a real directory")
        if hasattr(os, "geteuid") and owner_of(info) != os.geteuid():
            fail(f"{label} must remain owned by the current user")
        if prior is None:
            validate_builder_cache_directory_info(info, label)
        directory_fd = open_directory_fd(path)
        try:
            temporary_mode = OWNER_DIRECTORY_MODE if prior is None else prior.mode | 0o700
            os.fchmod(directory_fd, temporary_mode)
            validate_builder_cache_directory_info(os.fstat(directory_fd), label)
        finally:
            os.close(directory_fd)

    root = builder_cache_root(target, plugin_version)
    current_tree_invalid = False
    try:
        current_tree = capture_builder_cache_tree(
            root,
            "nddev-builder version cache before rollback",
        )
    except ConcurrentTargetChange:
        raise
    except (CodexSetupError, OSError):
        current_tree = None
        current_tree_invalid = True
    if current_tree_invalid or current_tree != expected.tree:
        if path_entry_exists(root):
            remove_builder_cache_tree(root, "nddev-builder version cache")
        if expected.tree is not None:
            create_builder_cache_tree(root, expected.tree)

    for index in range(len(ancestor_paths) - 1, -1, -1):
        path = ancestor_paths[index]
        prior = expected.ancestors[index]
        label = f"nddev-builder cache ancestor {index}"
        if prior is not None:
            if not path_entry_exists(path):
                fail(f"{label} disappeared during install-builder")
            info = require_directory(path, label)
            if identity_of(info) != prior.identity:
                fail_concurrent(f"{label} changed during install-builder")
            directory_fd = open_directory_fd(path)
            try:
                os.fchmod(directory_fd, prior.mode)
            finally:
                os.close(directory_fd)
            continue
        if not path_entry_exists(path):
            continue
        validate_builder_cache_directory(path, label)
        directory_fd = open_directory_fd(path)
        try:
            if os.listdir(directory_fd):
                fail_concurrent(f"{label} gained unrelated content during install-builder")
            current_identity = identity_of(os.fstat(directory_fd))
        finally:
            os.close(directory_fd)
        parent_fd = open_directory_fd(path.parent)
        try:
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if identity_of(current) != current_identity:
                fail_concurrent(f"{label} changed before rollback removal")
            os.rmdir(path.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    if capture_builder_cache_transaction(target, plugin_version) != expected:
        fail("nddev-builder cache rollback postcondition failed")


def validate_builder_plugin_result(
    result: dict[str, Any],
    target: Path,
    plugin_version: str,
) -> None:
    require_exact_keys(
        result,
        {"pluginId", "name", "marketplaceName", "version", "installedPath", "authPolicy"},
        "official Codex plugin-add result",
    )
    if (
        result["pluginId"] != BUILDER_PLUGIN_QUALIFIED_ID
        or result["name"] != BUILDER_PLUGIN_ID
        or result["marketplaceName"] != BUILDER_MARKETPLACE_ID
        or result["version"] != plugin_version
        or result["authPolicy"] != "ON_INSTALL"
    ):
        fail("official Codex plugin-add result has an invalid plugin identity")
    installed_path = result["installedPath"]
    if not isinstance(installed_path, str) or not Path(installed_path).is_absolute():
        fail("official Codex plugin-add result has an invalid installed path")
    try:
        actual = Path(installed_path).resolve(strict=True)
        expected = builder_cache_root(target, plugin_version).resolve(strict=True)
    except OSError as exc:
        fail(f"official Codex plugin-add installed path cannot be resolved: {exc}")
    if actual != expected:
        fail("official Codex plugin-add result is bound to the wrong cache path")


def capture_builder_files(
    target: Path,
) -> tuple[dict[str, FileSnapshot | None], dict[str, bytes | None]]:
    names = (*MANAGED_FILES, STAMP_NAME, BUILDER_PROFILE_NAME)
    snapshots: dict[str, FileSnapshot | None] = {}
    contents: dict[str, bytes | None] = {}
    for name in names:
        snapshot = snapshot_target_file(target, name, owner_only=False)
        snapshots[name] = snapshot
        if snapshot is None:
            contents[name] = None
            continue
        content, _ = read_target_file(
            target,
            name,
            f"builder transaction path {target / name}",
            owner_only=True,
            max_bytes=(
                METADATA_MAX_BYTES
                if name in {STAMP_NAME, BUILDER_PROFILE_NAME}
                else MANAGED_PAYLOAD_MAX_BYTES
            ),
        )
        contents[name] = content
    return snapshots, contents


def assert_builder_paths_unchanged(
    target: Path,
    expected: dict[str, FileSnapshot | None],
    names: tuple[str, ...],
) -> None:
    for name in names:
        if snapshot_target_file(target, name, owner_only=False) != expected[name]:
            fail(f"official Codex plugin command changed an unauthorized path: {target / name}")


def restore_builder_files(
    target: Path,
    original_snapshots: dict[str, FileSnapshot | None],
    original_contents: dict[str, bytes | None],
) -> None:
    names = (*MANAGED_FILES, STAMP_NAME, BUILDER_PROFILE_NAME)
    current: dict[str, FileSnapshot | None] = {}
    selected: list[str] = []
    for name in names:
        current[name] = snapshot_target_file(target, name, owner_only=False)
        if current[name] != original_snapshots[name]:
            selected.append(name)
    if selected:
        replace_managed_state(
            target,
            original_contents,
            current,
            names=tuple(selected),
        )
    for name in names:
        expected_content = original_contents[name]
        if expected_content is None:
            if snapshot_target_file(target, name, owner_only=False) is not None:
                fail(f"builder transaction rollback could not remove {target / name}")
            continue
        restored, _ = read_target_file(
            target,
            name,
            f"restored builder transaction path {target / name}",
            owner_only=True,
            max_bytes=(
                METADATA_MAX_BYTES
                if name in {STAMP_NAME, BUILDER_PROFILE_NAME}
                else MANAGED_PAYLOAD_MAX_BYTES
            ),
        )
        if restored != expected_content:
            fail(f"builder transaction rollback content mismatch for {target / name}")


def _enable_builder_in_config(
    target: Path,
    guard: TargetGuard,
    expected_profile: bytes,
    plugin_version: str,
) -> dict[str, Any]:
    """Add the canonical builder block to config.toml when the cache and profile
    are already current but the base-config enable is absent -- e.g. a setup
    apply or switch rewrote config.toml to the pure setup base. This restores the
    default-on builder for a plain ``codex`` launch without re-materializing the
    cache or invoking the official Codex plugin commands."""
    original_snapshots, original_contents = capture_builder_files(target)
    original_config = original_contents["config.toml"]
    if original_config is None:
        fail("managed config.toml disappeared from the builder transaction snapshot")
    guard.expected_managed = {
        name: original_snapshots[name] for name in (*MANAGED_FILES, STAMP_NAME)
    }
    guard.mutated_paths.clear()
    guard.manager_results.clear()
    try:
        current_config = snapshot_target_file(target, "config.toml", owner_only=False)
        desired: dict[str, bytes | None] = {
            "config.toml": config_with_builder_block(original_config, expected_profile),
        }
        replace_managed_state(
            target,
            desired,
            {"config.toml": current_config},
            names=("config.toml",),
        )
        require_effective_clean_managed(target)
        require_current_software(target)
        if not _builder_enabled_on_disk(target, expected_profile):
            fail("nddev-builder base-config enable postcondition failed")
        assert_builder_paths_unchanged(
            target,
            original_snapshots,
            ("AGENTS.md", STAMP_NAME, BUILDER_PROFILE_NAME),
        )
    except BaseException as operation_error:
        try:
            restore_builder_files(target, original_snapshots, original_contents)
            require_effective_clean_managed(target)
        except BaseException as rollback_error:
            raise CodexSetupError(
                "install-builder failed and configuration rollback also failed: "
                f"{type(operation_error).__name__}: {operation_error}"
            ) from rollback_error
        raise
    finally:
        guard.mutated_paths.clear()
        guard.manager_results.clear()
    return {
        "schema_version": 1,
        "command": "install-builder",
        "target": str(target),
        "changed": True,
        "plugin_version": plugin_version,
        "profile": BUILDER_PROFILE_NAME,
    }


def install_builder(target: Path) -> dict[str, Any]:
    plugin_version, source_manifest = builder_source_contract()
    expected_profile = builder_profile_bytes()
    with target_lock(target) as guard:
        require_effective_clean_managed(target)
        installation = require_current_software(target)
        profile_state = inspect_builder_profile(target, expected_profile)
        cache_state = inspect_builder_cache(target, plugin_version, source_manifest)
        if profile_state == "drifted":
            fail(f"existing {BUILDER_PROFILE_NAME} is not the canonical builder profile")
        if cache_state == "drifted":
            fail("current nddev-builder plugin cache manifest is invalid")
        if profile_state == "current" and cache_state == "current":
            if _builder_enabled_on_disk(target, expected_profile):
                revalidate_guard(guard, allow_missing=False)
                return {
                    "schema_version": 1,
                    "command": "install-builder",
                    "target": str(target),
                    "changed": False,
                    "plugin_version": plugin_version,
                    "profile": BUILDER_PROFILE_NAME,
                }
            return _enable_builder_in_config(target, guard, expected_profile, plugin_version)

        original_snapshots, original_contents = capture_builder_files(target)
        original_cache = capture_builder_cache_transaction(target, plugin_version)
        guard.expected_managed = {
            name: original_snapshots[name] for name in (*MANAGED_FILES, STAMP_NAME)
        }
        guard.mutated_paths.clear()
        guard.manager_results.clear()
        try:
            marketplace_result = run_builder_plugin_command(
                installation,
                target,
                ["plugin", "marketplace", "add", str(ROOT), "--json"],
            )
            validate_builder_marketplace_result(marketplace_result)
            revalidate_guard(guard, allow_missing=False)
            assert_builder_paths_unchanged(
                target,
                original_snapshots,
                ("AGENTS.md", STAMP_NAME, BUILDER_PROFILE_NAME),
            )
            installation = require_current_software(target)
            plugin_result = run_builder_plugin_command(
                installation,
                target,
                ["plugin", "add", BUILDER_PLUGIN_QUALIFIED_ID, "--json"],
            )
            validate_builder_plugin_result(plugin_result, target, plugin_version)
            revalidate_guard(guard, allow_missing=False)
            assert_builder_paths_unchanged(
                target,
                original_snapshots,
                ("AGENTS.md", STAMP_NAME, BUILDER_PROFILE_NAME),
            )
            mutated_config, _ = read_target_file(
                target,
                "config.toml",
                f"official Codex plugin configuration {target / 'config.toml'}",
                max_bytes=METADATA_MAX_BYTES,
            )
            original_config = original_contents["config.toml"]
            if original_config is None:
                fail("managed config.toml disappeared from the builder transaction snapshot")
            validate_builder_mutated_config(mutated_config, original_config)
            if inspect_builder_cache(target, plugin_version, source_manifest) != "current":
                fail("official Codex plugin installation did not produce the pinned cache manifest")

            current_config = snapshot_target_file(target, "config.toml", owner_only=False)
            current_profile = snapshot_target_file(target, BUILDER_PROFILE_NAME, owner_only=False)
            desired: dict[str, bytes | None] = {
                "config.toml": config_with_builder_block(original_config, expected_profile),
                BUILDER_PROFILE_NAME: expected_profile,
            }
            expected = {
                "config.toml": current_config,
                BUILDER_PROFILE_NAME: current_profile,
            }
            replace_managed_state(
                target,
                desired,
                expected,
                names=("config.toml", BUILDER_PROFILE_NAME),
            )
            require_effective_clean_managed(target)
            require_current_software(target)
            if inspect_builder_profile(target, expected_profile) != "current":
                fail("nddev-builder profile installation postcondition failed")
            if inspect_builder_cache(target, plugin_version, source_manifest) != "current":
                fail("nddev-builder cache installation postcondition failed")
            if not _builder_enabled_on_disk(target, expected_profile):
                fail("nddev-builder base-config enable postcondition failed")
            assert_builder_paths_unchanged(
                target,
                original_snapshots,
                ("AGENTS.md", STAMP_NAME),
            )
        except BaseException as operation_error:
            try:
                restore_builder_files(target, original_snapshots, original_contents)
                restore_builder_cache_transaction(
                    target,
                    plugin_version,
                    original_cache,
                )
                require_effective_clean_managed(target)
            except BaseException as rollback_error:
                raise CodexSetupError(
                    "install-builder failed and configuration/cache rollback also failed: "
                    f"{type(operation_error).__name__}: {operation_error}"
                ) from rollback_error
            raise
        finally:
            guard.mutated_paths.clear()
            guard.manager_results.clear()
    return {
        "schema_version": 1,
        "command": "install-builder",
        "target": str(target),
        "changed": True,
        "plugin_version": plugin_version,
        "profile": BUILDER_PROFILE_NAME,
    }


def install_or_update_cli(target: Path, command: str) -> dict[str, Any]:
    before = inspect_software_installation(target)
    if before is not None and before.version == TESTED_CODEX_VERSION:
        return {
            "schema_version": 1,
            "command": command,
            "target": str(target),
            "changed": False,
            "version": before.version,
            "executable": str(before.executable),
        }
    if command == "install-cli" and before is not None:
        fail("another Codex CLI version is installed; use update-cli")
    if command == "update-cli" and before is None:
        fail("Codex CLI is not installed at the selected target; use install-cli")

    with target_lock(target):
        ensure_target_directory(target, create=True)
        current = inspect_software_installation(target)
        if command == "install-cli" and current is not None:
            fail("Codex CLI appeared concurrently; use update-cli")
        if command == "update-cli" and current is None:
            fail("Codex CLI disappeared before update")
        with tempfile.TemporaryDirectory(prefix="nddev-codex-installer-") as raw:
            workspace = Path(raw)
            installer = workspace / INSTALLER_NAME
            download_verified_installer(installer)
            run_verified_installer(installer, target, workspace)
        installation = require_current_software(target)
    return {
        "schema_version": 1,
        "command": command,
        "target": str(target),
        "changed": True,
        "version": installation.version,
        "executable": str(installation.executable),
    }


def spawn_codex_child(
    executable: str,
    child_args: list[str],
    environment: dict[str, str],
) -> int:
    try:
        completed = subprocess.run(
            [executable, *child_args],
            env=environment,
            check=False,
        )
    except FileNotFoundError:
        fail("codex executable disappeared before launch")
    if completed.returncode < 0:
        return 128 + abs(completed.returncode)
    return completed.returncode


def launch_codex(target: Path, child_args: list[str]) -> int:
    forwarded = child_args[1:] if child_args[:1] == ["--"] else child_args
    with target_lock(target) as guard:
        require_effective_clean_managed(target)
        require_current_software(target)
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(target)
        require_effective_clean_managed(target)
        installation = require_current_software(target)
        revalidate_guard(guard, allow_missing=False)
        return spawn_codex_child(str(installation.executable), forwarded, environment)


def resolve_desktop_workspace(raw_workspace: str | None) -> Path | None:
    if raw_workspace is None:
        return None
    workspace = Path(raw_workspace).expanduser()
    if not workspace.is_absolute():
        fail("--workspace must be an absolute path")
    try:
        info = workspace.lstat()
    except FileNotFoundError:
        fail("--workspace must already exist")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail("--workspace must be a real directory")
    return workspace.resolve(strict=True)


def launch_desktop(target: Path, raw_workspace: str | None) -> int:
    if sys.platform != "darwin":
        fail("the official `codex app` desktop bridge is supported only on macOS")
    workspace = resolve_desktop_workspace(raw_workspace)
    with target_lock(target) as guard:
        require_current_software(target)
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(target)
        installation = require_current_software(target)
        revalidate_guard(guard, allow_missing=False)
        return spawn_codex_child(
            str(installation.executable),
            ["app", *([str(workspace)] if workspace is not None else [])],
            environment,
        )


def human_output(value: dict[str, Any]) -> str:
    command = value.get("command")
    if command == "list":
        return "\n".join(f"{item['id']}: {item['description']}" for item in value["setups"])
    if command == "status":
        setup = f" ({value['setup_id']})" if value["setup_id"] else ""
        drift = f"; drift={','.join(value['drift'])}" if value["drift"] else ""
        override = f"; {OVERRIDE_NAME}=present" if value["agents_override_present"] else ""
        return f"{value['state']}{setup}: {value['target']}{drift}{override}"
    if command == "builder-status":
        return (
            f"{value['state']}: {value['target']}; "
            f"enabled={value['config_enabled']}; "
            f"profile={value['profile_state']}; cache={value['cache_state']}; "
            f"plugin={value['plugin_version']}"
        )
    if command == "plan":
        changes = ", ".join(value["changes"]) or "none"
        return f"{value['operation']} {value['setup_id']} at {value['target']}; changes: {changes}"
    return json.dumps(value, indent=2, sort_keys=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nddev-codex",
        description="Manage a portable Codex setup at an explicit target.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List source setups.")
    list_parser.add_argument("--json", action="store_true", dest="output_json")

    status_parser = subparsers.add_parser("status", help="Inspect an explicit target.")
    add_target(status_parser)

    for command in ("plan", "apply", "switch"):
        command_parser = subparsers.add_parser(command, help=f"{command.title()} a setup.")
        command_parser.add_argument("--setup", required=True)
        add_target(command_parser)

    restore_parser = subparsers.add_parser("restore", help="Restore a target-bound backup.")
    restore_parser.add_argument("--backup", required=True, type=int, choices=range(10))
    add_target(restore_parser)

    remove_parser = subparsers.add_parser("remove", help="Remove only managed setup files.")
    add_target(remove_parser)

    for command, help_text in (
        ("software-status", "Inspect target-owned Codex CLI software."),
        ("install-cli", "Install the pinned official Codex CLI release."),
        ("update-cli", "Update target-owned Codex CLI to the pinned release."),
        ("builder-status", "Inspect the isolated nddev-builder plugin profile."),
        ("install-builder", "Install nddev-builder without changing the setup config."),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        add_target(command_parser)

    launch_parser = subparsers.add_parser(
        "launch", help="Launch Codex with an isolated managed CODEX_HOME."
    )
    add_target(launch_parser)
    launch_parser.add_argument(
        "codex_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to Codex after --.",
    )

    desktop_parser = subparsers.add_parser(
        "desktop", help="Delegate desktop launch or install to official `codex app`."
    )
    add_target(desktop_parser)
    desktop_parser.add_argument(
        "--workspace",
        help="Optional absolute workspace directory passed to `codex app`.",
    )
    return parser


def add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True, help="Absolute Codex home path.")
    parser.add_argument("--json", action="store_true", dest="output_json")


def run(args: argparse.Namespace) -> dict[str, Any] | int:
    if args.command == "list":
        return {"schema_version": 1, "command": "list", "setups": list_setups()}
    target = resolve_target(args.target)
    if args.command == "status":
        return {
            "schema_version": 1,
            "command": "status",
            "target": str(target),
            **inspect_target(target),
        }
    if args.command == "plan":
        return plan_setup(target, args.setup)
    if args.command in {"apply", "switch"}:
        return mutate_setup(target, args.setup, args.command)
    if args.command == "restore":
        return restore_slot(target, args.backup)
    if args.command == "remove":
        return remove_setup(target)
    if args.command == "software-status":
        return software_status(target)
    if args.command in {"install-cli", "update-cli"}:
        return install_or_update_cli(target, args.command)
    if args.command == "builder-status":
        return builder_status(target)
    if args.command == "install-builder":
        return install_builder(target)
    if args.command == "launch":
        return launch_codex(target, list(args.codex_args))
    if args.command == "desktop":
        return launch_desktop(target, args.workspace)
    fail(f"unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (CodexSetupError, OSError) as exc:
        if isinstance(exc, OSError):
            detail = exc.strerror or type(exc).__name__
            error_message = f"filesystem operation failed: {detail}"
            if exc.filename is not None:
                error_message += f" ({exc.filename})"
        else:
            error_message = str(exc)
        if getattr(args, "output_json", False):
            print(json.dumps({"schema_version": 1, "error": error_message}, sort_keys=True))
        else:
            print(f"nddev-codex: error: {error_message}", file=sys.stderr)
        return 2
    if isinstance(result, int):
        return result
    if getattr(args, "output_json", False):
        sys.stdout.buffer.write(canonical_json(result))
    else:
        print(human_output(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
