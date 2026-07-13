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
import shutil
import stat
import subprocess
import sys
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
OWNER_FILE_MODE = 0o600
OWNER_DIRECTORY_MODE = 0o700
METADATA_MAX_BYTES = 256 * 1024
MANAGED_PAYLOAD_MAX_BYTES = 8 * 1024 * 1024
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


ACTIVE_TARGET_GUARD: contextvars.ContextVar[TargetGuard | None] = (
    contextvars.ContextVar("nddev_codex_target_guard", default=None)
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


def require_regular_file(
    path: Path, label: str, *, owner_only: bool = False
) -> os.stat_result:
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


def load_json_object(
    path: Path, label: str, *, owner_only: bool = False
) -> dict[str, Any]:
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
    if (
        not isinstance(metadata["description"], str)
        or not metadata["description"].strip()
    ):
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
            fail_concurrent(
                "canonical --target parent handle changed during the operation"
            )


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


def snapshot_target_file(
    target: Path, name: str, *, owner_only: bool
) -> FileSnapshot | None:
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


def assert_target_snapshot(
    target: Path, name: str, expected: FileSnapshot | None
) -> None:
    actual = snapshot_target_file(target, name, owner_only=False)
    if actual != expected:
        fail_concurrent(f"managed path changed concurrently: {target / name}")


def assert_managed_snapshot(
    target: Path, expected: dict[str, FileSnapshot | None]
) -> None:
    for name in (*MANAGED_FILES, STAMP_NAME):
        assert_target_snapshot(target, name, expected[name])


def target_has_instruction_override(target: Path) -> bool:
    return target_entry_present(target, OVERRIDE_NAME)


def reject_instruction_override(target: Path, action: str) -> None:
    if target_has_instruction_override(target):
        fail(
            f"{OVERRIDE_NAME} shadows the managed AGENTS.md; remove it before {action}"
        )


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

    expected = validate_digest_map(
        stamp["managed_files"], "managed stamp managed_files"
    )
    drift: list[str] = []
    for name in MANAGED_FILES:
        snapshot = snapshot_target_file(target, name, owner_only=False)
        if expected[name] is None or snapshot is None:
            drift.append(name)
            continue
        owner_matches = not hasattr(os, "geteuid") or snapshot.owner == os.geteuid()
        if (
            snapshot.digest != expected[name]
            or snapshot.mode != OWNER_FILE_MODE
            or not owner_matches
        ):
            drift.append(name)
    stamp_snapshot = snapshot_target_file(target, STAMP_NAME, owner_only=False)
    if stamp_snapshot is None:
        drift.append(STAMP_NAME)
    else:
        stamp_owner_matches = (
            not hasattr(os, "geteuid") or stamp_snapshot.owner == os.geteuid()
        )
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
            max_bytes=(
                METADATA_MAX_BYTES if name == STAMP_NAME else MANAGED_PAYLOAD_MAX_BYTES
            ),
        )
        if actual_content != expected_content:
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
            "managed_files": {
                name: sha256_bytes(rendered[name]) for name in MANAGED_FILES
            },
        }
    )


def backup_pool(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-codex-backups"


def lock_path(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-codex.lock"


@contextlib.contextmanager
def target_lock(target: Path) -> Iterator[TargetGuard]:
    if not anchored_directory_operations_supported():
        fail(
            "mutating commands require dir-fd and no-follow filesystem support "
            "on this platform"
        )

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
            if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISDIR(
                target_info.st_mode
            ):
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
        owner = canonical_json(
            {"schema_version": 1, "pid": os.getpid(), "target": str(target)}
        )
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
                current_lock = os.stat(
                    lock.name, dir_fd=parent_fd, follow_symlinks=False
                )
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
        except BaseException as exc:
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
                    fail(
                        f"backup cleanup encountered unexpected payload entries: {name}"
                    )
                for payload_entry in payload_entries:
                    payload_info = os.stat(
                        payload_entry,
                        dir_fd=payload_fd,
                        follow_symlinks=False,
                    )
                    if stat.S_ISLNK(payload_info.st_mode) or not stat.S_ISREG(
                        payload_info.st_mode
                    ):
                        fail(
                            "backup cleanup encountered an unsafe payload entry: "
                            f"{payload_entry}"
                        )
            finally:
                os.close(payload_fd)
        if BACKUP_NAME in slot_entries:
            envelope_info = os.stat(
                BACKUP_NAME,
                dir_fd=slot_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(envelope_info.st_mode) or not stat.S_ISREG(
                envelope_info.st_mode
            ):
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
        except BaseException as exc:
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
    allowed_names = set((*MANAGED_FILES, STAMP_NAME))
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
                fail_concurrent(
                    f"managed path appeared before replacement: {target / name}"
                )

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
                fail_concurrent(
                    f"managed path appeared during replacement: {target / name}"
                )
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
                    fail_concurrent(
                        f"managed path appeared after removal: {target / name}"
                    )
            elif installed is None or installed.digest != sha256_bytes(content):
                fail_concurrent(
                    f"managed path changed after replacement: {target / name}"
                )
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
            desired: dict[str, bytes | None] = {
                name: None for name in (*MANAGED_FILES, STAMP_NAME)
            }
            allowed_payload = {
                name for name, digest in digests.items() if digest is not None
            }
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
        desired: dict[str, bytes | None] = {
            name: None for name in (*MANAGED_FILES, STAMP_NAME)
        }
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
                    fail_concurrent(
                        f"new backup slot changed during recovery: {destination}"
                    )
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
        if (
            old_quarantined
            and old_identity is not None
            and entry_exists_at(pool_fd, hold_name)
        ):
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


def apply_rendered(target: Path, setup_id: str, rendered: dict[str, bytes]) -> None:
    guard = current_target_guard(target)
    if guard is None:
        fail("apply requires an active anchored target lock")
    expected = guard.expected_managed
    if expected is None:
        expected = capture_managed_snapshot(target)
    ensure_target_directory(target, create=True)
    desired: dict[str, bytes | None] = {
        **rendered,
        STAMP_NAME: stamp_bytes(target, setup_id, rendered),
    }
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
    expected: dict[str, FileSnapshot | None] = {
        name: None for name in (*MANAGED_FILES, STAMP_NAME)
    }
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
        pass
    return tuple(selected)


def cleanup_unbacked_mutation(target: Path) -> None:
    empty: dict[str, bytes | None] = {
        name: None for name in (*MANAGED_FILES, STAMP_NAME)
    }
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
            backup_slot, rollback_desired, backup_lease = create_transaction_backup(
                target
            )
        try:
            if backup_lease is not None:
                validate_backup_before_target_mutation(backup_lease)
                assert_managed_snapshot(target, before)
            apply_rendered(target, setup_id, rendered)
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
    status = require_clean_managed(target)
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
            desired: dict[str, bytes | None] = {
                name: None for name in (*MANAGED_FILES, STAMP_NAME)
            }
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
        executable = shutil.which("codex")
        if executable is None:
            fail("codex executable was not found on PATH")
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(target)
        require_effective_clean_managed(target)
        revalidate_guard(guard, allow_missing=False)
        return spawn_codex_child(executable, forwarded, environment)


def human_output(value: dict[str, Any]) -> str:
    command = value.get("command")
    if command == "list":
        return "\n".join(
            f"{item['id']}: {item['description']}" for item in value["setups"]
        )
    if command == "status":
        setup = f" ({value['setup_id']})" if value["setup_id"] else ""
        drift = f"; drift={','.join(value['drift'])}" if value["drift"] else ""
        override = (
            f"; {OVERRIDE_NAME}=present" if value["agents_override_present"] else ""
        )
        return f"{value['state']}{setup}: {value['target']}{drift}{override}"
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
        command_parser = subparsers.add_parser(
            command, help=f"{command.title()} a setup."
        )
        command_parser.add_argument("--setup", required=True)
        add_target(command_parser)

    restore_parser = subparsers.add_parser(
        "restore", help="Restore a target-bound backup."
    )
    restore_parser.add_argument("--backup", required=True, type=int, choices=range(10))
    add_target(restore_parser)

    remove_parser = subparsers.add_parser(
        "remove", help="Remove only managed setup files."
    )
    add_target(remove_parser)

    launch_parser = subparsers.add_parser(
        "launch", help="Launch Codex with an isolated managed CODEX_HOME."
    )
    add_target(launch_parser)
    launch_parser.add_argument(
        "codex_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to Codex after --.",
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
    if args.command == "launch":
        return launch_codex(target, list(args.codex_args))
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
            print(
                json.dumps(
                    {"schema_version": 1, "error": error_message}, sort_keys=True
                )
            )
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
