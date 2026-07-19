#!/usr/bin/env python3
"""Create conservative, Codex-native artifact skeletons without dependencies."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, NoReturn
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
DARWIN_SYSTEM_ALIASES = {
    Path("/etc"): Path("/private/etc"),
    Path("/tmp"): Path("/private/tmp"),
    Path("/var"): Path("/private/var"),
}
DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
FILE_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
FILE_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class CreationError(Exception):
    """A stable user-facing creation failure."""


@dataclass(frozen=True)
class PlannedFile:
    path: Path
    content: bytes


@dataclass(frozen=True)
class CreationPlan:
    files: tuple[PlannedFile, ...]
    directories: tuple[Path, ...] = ()

    @property
    def paths(self) -> list[Path]:
        return [planned.path for planned in self.files]


@dataclass(frozen=True)
class FileSnapshot:
    exists: bool
    identity: tuple[int, int] | None = None
    signature: tuple[int, int, int, int, int, int] | None = None
    content: bytes = b""
    mode: int = 0


@dataclass
class CreatedDirectory:
    parent_fd: int
    name: str
    path: Path
    identity: tuple[int, int]


@dataclass
class StagedFile:
    planned: PlannedFile
    parent_fd: int
    parent_path: Path
    target_name: str
    temp_name: str
    identity: tuple[int, int]
    snapshot: FileSnapshot
    temp_present: bool = True


@dataclass(frozen=True)
class CommittedFile:
    staged: StagedFile
    identity: tuple[int, int]


TransactionHook = Callable[[str, int, Path], None]


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
        except OSError as exc:
            fail(f"cannot inspect output path component {current}: {exc}")
        if stat.S_ISLNK(info.st_mode):
            if sys.platform == "darwin" and current in DARWIN_SYSTEM_ALIASES:
                continue
            fail(f"output path contains a symlink component: {current}")
    if sys.platform == "darwin":
        for alias, real_path in DARWIN_SYSTEM_ALIASES.items():
            try:
                relative = lexical.relative_to(alias)
            except ValueError:
                continue
            return real_path / relative
    return lexical


def reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            fail(f"cannot inspect path component {current}: {exc}")
        if stat.S_ISLNK(info.st_mode):
            fail(f"path contains a symlink component: {current}")


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _signature(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _validate_directory_binding(path: Path, descriptor: int) -> None:
    reject_symlink_components(path)
    try:
        path_info = os.stat(path, follow_symlinks=False)
        descriptor_info = os.fstat(descriptor)
    except OSError as exc:
        fail(f"output directory binding changed for {path}: {exc}")
    if (
        not stat.S_ISDIR(path_info.st_mode)
        or not stat.S_ISDIR(descriptor_info.st_mode)
        or _identity(path_info) != _identity(descriptor_info)
    ):
        fail(f"output directory binding changed for {path}")


def _remove_created_directory(
    parent_fd: int,
    name: str,
    path: Path,
    identity: tuple[int, int],
) -> str | None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot inspect created directory {path}: {exc}"
    if _identity(current) != identity or not stat.S_ISDIR(current.st_mode):
        return f"refusing to remove changed directory {path}"
    try:
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as exc:
        return f"cannot remove created directory {path}: {exc}"
    return None


def _open_directory_anchored(
    path: Path,
    created_directories: list[CreatedDirectory],
) -> int:
    if not path.is_absolute():
        fail(f"output directory must be absolute: {path}")
    try:
        descriptor = os.open(path.anchor, DIRECTORY_OPEN_FLAGS)
    except OSError as exc:
        fail(f"cannot open output filesystem root for {path}: {exc}")
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            created_now = False
            try:
                entry_info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    created_now = True
                except FileExistsError:
                    created_now = False
                except OSError as exc:
                    fail(f"cannot create output directory {current / part}: {exc}")
                try:
                    entry_info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                except OSError as exc:
                    fail(f"cannot inspect created output directory {current / part}: {exc}")
            except OSError as exc:
                fail(f"cannot inspect output directory {current / part}: {exc}")
            if stat.S_ISLNK(entry_info.st_mode) or not stat.S_ISDIR(entry_info.st_mode):
                fail(f"output path must contain only real directories: {current / part}")
            try:
                child_descriptor = os.open(part, DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                fail(f"cannot open output directory {current / part}: {exc}")
            try:
                opened_info = os.fstat(child_descriptor)
                final_entry_info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened_info.st_mode)
                    or _identity(entry_info) != _identity(opened_info)
                    or _identity(final_entry_info) != _identity(opened_info)
                ):
                    fail(f"output directory changed while opening: {current / part}")
                if created_now:
                    created_identity = _identity(opened_info)
                    try:
                        journal_parent_fd = os.dup(descriptor)
                    except BaseException as original:
                        cleanup_error = _remove_created_directory(
                            descriptor,
                            part,
                            current / part,
                            created_identity,
                        )
                        if cleanup_error is not None:
                            raise original from CreationError(cleanup_error)
                        raise
                    created_directories.append(
                        CreatedDirectory(
                            parent_fd=journal_parent_fd,
                            name=part,
                            path=current / part,
                            identity=created_identity,
                        )
                    )
                    os.fchmod(child_descriptor, 0o700)
                    secured_info = os.fstat(child_descriptor)
                    if (
                        _identity(secured_info) != created_identity
                        or stat.S_IMODE(secured_info.st_mode) != 0o700
                    ):
                        fail(f"created output directory invariants failed: {current / part}")
            except BaseException:
                os.close(child_descriptor)
                raise
            os.close(descriptor)
            descriptor = child_descriptor
            current /= part
        _validate_directory_binding(path, descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


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


def _planned_bytes(path: Path, content: bytes) -> PlannedFile:
    if not content or len(content) > MAX_TEXT_BYTES:
        fail(f"generated artifact has an invalid size: {path}")
    return PlannedFile(path=path, content=content)


def _planned_text(path: Path, content: str) -> PlannedFile:
    if not content.endswith("\n") or "\r" in content:
        fail(f"generated text must be LF-terminated: {path}")
    return _planned_bytes(path, content.encode("utf-8"))


def _planned_json(path: Path, value: dict[str, Any]) -> PlannedFile:
    return _planned_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _read_snapshot(parent_fd: int, target_name: str, path: Path) -> FileSnapshot:
    try:
        descriptor = os.open(target_name, FILE_READ_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return FileSnapshot(exists=False)
    except OSError as exc:
        fail(f"refusing to replace non-regular or unreadable path {path}: {exc}")
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            fail(f"refusing to replace non-regular path: {path}")
        if initial.st_size > MAX_TEXT_BYTES:
            fail(f"existing artifact exceeds the rollback limit of {MAX_TEXT_BYTES} bytes: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = os.read(descriptor, min(64 * 1024, MAX_TEXT_BYTES + 1 - total))
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_TEXT_BYTES:
                fail(f"existing artifact grew beyond the rollback limit: {path}")
        final = os.fstat(descriptor)
        try:
            bound = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            fail(f"existing artifact changed while being snapshotted {path}: {exc}")
        if (
            total != initial.st_size
            or _signature(initial) != _signature(final)
            or _signature(initial) != _signature(bound)
        ):
            fail(f"existing artifact changed while being snapshotted: {path}")
        return FileSnapshot(
            exists=True,
            identity=_identity(initial),
            signature=_signature(initial),
            content=b"".join(chunks),
            mode=stat.S_IMODE(initial.st_mode),
        )
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        try:
            written = os.write(descriptor, view)
        except InterruptedError:
            continue
        if written <= 0:
            fail("cannot make progress while staging generated content")
        view = view[written:]


def _unlink_known_file(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
) -> str | None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot inspect staging file {name}: {exc}"
    if _identity(current) != identity or not stat.S_ISREG(current.st_mode):
        return f"refusing to remove changed staging file {name}"
    try:
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as exc:
        return f"cannot remove staging file {name}: {exc}"
    return None


def _stage_bytes(
    parent_fd: int,
    target_name: str,
    content: bytes,
    *,
    mode: int,
) -> tuple[str, tuple[int, int]]:
    if len(content) > MAX_TEXT_BYTES:
        fail(f"staged artifact exceeds {MAX_TEXT_BYTES} bytes")
    descriptor: int | None = None
    temp_name = ""
    for _attempt in range(64):
        temp_name = f".{target_name[:48]}.{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(
                temp_name,
                FILE_CREATE_FLAGS,
                0o600,
                dir_fd=parent_fd,
            )
            break
        except FileExistsError:
            continue
        except OSError as exc:
            fail(f"cannot create anchored staging file for {target_name}: {exc}")
    if descriptor is None:
        fail(f"cannot allocate a unique staging file for {target_name}")
    try:
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_uid != os.getuid()
            or initial.st_nlink != 1
        ):
            fail(f"initial staging file invariants failed for {target_name}")
        initial_identity = _identity(initial)
    except BaseException:
        os.close(descriptor)
        raise
    try:
        _write_all(descriptor, content)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or _identity(info) != initial_identity
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_size != len(content)
        ):
            fail(f"staged artifact invariants failed for {target_name}")
        identity = _identity(info)
    except BaseException as original:
        os.close(descriptor)
        cleanup_error = _unlink_known_file(parent_fd, temp_name, initial_identity)
        if cleanup_error is not None:
            raise original from CreationError(cleanup_error)
        raise
    os.close(descriptor)
    try:
        bound = os.stat(temp_name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(bound) != identity or not stat.S_ISREG(bound.st_mode):
            fail(f"staged artifact binding changed for {target_name}")
    except BaseException as original:
        cleanup_error = _unlink_known_file(parent_fd, temp_name, identity)
        if cleanup_error is not None:
            raise original from CreationError(cleanup_error)
        raise
    return temp_name, identity


def _revalidate_snapshot(staged: StagedFile) -> None:
    try:
        current = os.stat(
            staged.target_name,
            dir_fd=staged.parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if staged.snapshot.exists:
            fail(f"target changed before commit: {staged.planned.path}")
        return
    except OSError as exc:
        fail(f"cannot revalidate target {staged.planned.path}: {exc}")
    if not staged.snapshot.exists or _signature(current) != staged.snapshot.signature:
        fail(f"target changed before commit: {staged.planned.path}")


def _revalidate_staged_file(staged: StagedFile) -> None:
    try:
        current = os.stat(
            staged.temp_name,
            dir_fd=staged.parent_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        fail(f"cannot revalidate staging file for {staged.planned.path}: {exc}")
    if (
        _identity(current) != staged.identity
        or not stat.S_ISREG(current.st_mode)
        or current.st_uid != os.getuid()
        or current.st_nlink != 1
        or stat.S_IMODE(current.st_mode) != 0o600
        or current.st_size != len(staged.planned.content)
    ):
        fail(f"staging file changed before commit: {staged.planned.path}")


def _validate_published(staged: StagedFile) -> tuple[int, int]:
    try:
        info = os.stat(
            staged.target_name,
            dir_fd=staged.parent_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        fail(f"published artifact cannot be inspected {staged.planned.path}: {exc}")
    if (
        not stat.S_ISREG(info.st_mode)
        or _identity(info) != staged.identity
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size != len(staged.planned.content)
    ):
        fail(f"published artifact invariants failed: {staged.planned.path}")
    return _identity(info)


def _cleanup_temp(staged: StagedFile) -> str | None:
    if not staged.temp_present:
        return None
    error = _unlink_known_file(staged.parent_fd, staged.temp_name, staged.identity)
    if error is None:
        staged.temp_present = False
    return error


def _restore_snapshot(committed: CommittedFile) -> str | None:
    staged = committed.staged
    restore_name: str | None = None
    restore_identity: tuple[int, int] | None = None
    try:
        current = os.stat(
            staged.target_name,
            dir_fd=staged.parent_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        return f"cannot inspect committed target {staged.planned.path}: {exc}"
    if _identity(current) != committed.identity or not stat.S_ISREG(current.st_mode):
        return f"refusing to clobber concurrently changed target {staged.planned.path}"
    if not staged.snapshot.exists:
        try:
            os.unlink(staged.target_name, dir_fd=staged.parent_fd)
            os.fsync(staged.parent_fd)
        except OSError as exc:
            return f"cannot remove fresh target {staged.planned.path}: {exc}"
        return None
    try:
        restore_name, restore_identity = _stage_bytes(
            staged.parent_fd,
            staged.target_name,
            staged.snapshot.content,
            mode=staged.snapshot.mode,
        )
        current = os.stat(
            staged.target_name,
            dir_fd=staged.parent_fd,
            follow_symlinks=False,
        )
        if _identity(current) != committed.identity:
            raise CreationError(
                f"refusing to clobber concurrently changed target {staged.planned.path}"
            )
        restore_current = os.stat(
            restore_name,
            dir_fd=staged.parent_fd,
            follow_symlinks=False,
        )
        if (
            restore_identity is None
            or _identity(restore_current) != restore_identity
            or not stat.S_ISREG(restore_current.st_mode)
        ):
            raise CreationError(f"restore staging file changed for {staged.planned.path}")
        os.replace(
            restore_name,
            staged.target_name,
            src_dir_fd=staged.parent_fd,
            dst_dir_fd=staged.parent_fd,
        )
        restored = os.stat(
            staged.target_name,
            dir_fd=staged.parent_fd,
            follow_symlinks=False,
        )
        if (
            _identity(restored) != restore_identity
            or stat.S_IMODE(restored.st_mode) != staged.snapshot.mode
            or restored.st_size != len(staged.snapshot.content)
        ):
            raise CreationError(f"restored artifact invariants failed: {staged.planned.path}")
        os.fsync(staged.parent_fd)
    except Exception as exc:
        if restore_name is not None and restore_identity is not None:
            cleanup_error = _unlink_known_file(
                staged.parent_fd,
                restore_name,
                restore_identity,
            )
            if cleanup_error is not None:
                return f"{exc}; {cleanup_error}"
        return str(exc)
    return None


def _cleanup_created_directories(created_directories: list[CreatedDirectory]) -> list[str]:
    errors: list[str] = []
    for created in reversed(created_directories):
        error = _remove_created_directory(
            created.parent_fd,
            created.name,
            created.path,
            created.identity,
        )
        if error is not None:
            errors.append(error)
    return errors


def _close_transaction_descriptors(
    parent_descriptors: dict[Path, int],
    created_directories: list[CreatedDirectory],
) -> None:
    for descriptor in parent_descriptors.values():
        os.close(descriptor)
    for created in created_directories:
        os.close(created.parent_fd)


def _apply_plan(
    plan: CreationPlan,
    *,
    force: bool,
    hook: TransactionHook | None = None,
) -> list[Path]:
    paths = plan.paths
    if len(set(paths)) != len(paths):
        fail("creation plan contains duplicate target paths")
    if any(not path.is_absolute() or path.name in {"", ".", ".."} for path in paths):
        fail("creation plan targets must be absolute file paths")
    if any(not path.is_absolute() for path in plan.directories):
        fail("creation plan directories must be absolute paths")
    created_directories: list[CreatedDirectory] = []
    parent_descriptors: dict[Path, int] = {}
    staged_files: list[StagedFile] = []
    committed_files: list[CommittedFile] = []
    recovery_errors: list[str] = []
    try:
        required_directories = set(plan.directories) | {path.parent for path in paths}
        for directory in sorted(
            required_directories, key=lambda item: (len(item.parts), str(item))
        ):
            descriptor = _open_directory_anchored(directory, created_directories)
            registered = False
            try:
                parent_descriptors[directory] = descriptor
                registered = True
            finally:
                if not registered:
                    os.close(descriptor)
        snapshots: dict[Path, FileSnapshot] = {}
        for planned in plan.files:
            parent_fd = parent_descriptors[planned.path.parent]
            _validate_directory_binding(planned.path.parent, parent_fd)
            snapshot = _read_snapshot(parent_fd, planned.path.name, planned.path)
            if snapshot.exists and not force:
                fail(f"artifact already exists: {planned.path}; pass --force to replace it")
            snapshots[planned.path] = snapshot
        for planned in plan.files:
            parent_fd = parent_descriptors[planned.path.parent]
            temp_name, identity = _stage_bytes(
                parent_fd,
                planned.path.name,
                planned.content,
                mode=0o600,
            )
            staged_files.append(
                StagedFile(
                    planned=planned,
                    parent_fd=parent_fd,
                    parent_path=planned.path.parent,
                    target_name=planned.path.name,
                    temp_name=temp_name,
                    identity=identity,
                    snapshot=snapshots[planned.path],
                )
            )
        for index, staged in enumerate(staged_files):
            if hook is not None:
                hook("before-commit", index, staged.planned.path)
            _validate_directory_binding(staged.parent_path, staged.parent_fd)
            _revalidate_staged_file(staged)
            _revalidate_snapshot(staged)
            if staged.snapshot.exists:
                os.replace(
                    staged.temp_name,
                    staged.target_name,
                    src_dir_fd=staged.parent_fd,
                    dst_dir_fd=staged.parent_fd,
                )
                staged.temp_present = False
            else:
                os.link(
                    staged.temp_name,
                    staged.target_name,
                    src_dir_fd=staged.parent_fd,
                    dst_dir_fd=staged.parent_fd,
                    follow_symlinks=False,
                )
            committed = CommittedFile(staged=staged, identity=staged.identity)
            committed_files.append(committed)
            if hook is not None:
                hook("after-publish", index, staged.planned.path)
            if staged.temp_present:
                cleanup_error = _unlink_known_file(
                    staged.parent_fd,
                    staged.temp_name,
                    staged.identity,
                )
                if cleanup_error is not None:
                    fail(cleanup_error)
                staged.temp_present = False
            _validate_directory_binding(staged.parent_path, staged.parent_fd)
            _validate_published(staged)
            os.fsync(staged.parent_fd)
        return paths
    except BaseException as exc:
        for committed in reversed(committed_files):
            error = _restore_snapshot(committed)
            if error is not None:
                recovery_errors.append(error)
        for staged in reversed(staged_files):
            error = _cleanup_temp(staged)
            if error is not None:
                recovery_errors.append(error)
        recovery_errors.extend(_cleanup_created_directories(created_directories))
        message = str(exc)
        if recovery_errors:
            message = f"{message}; rollback failed: {'; '.join(recovery_errors)}"
            raise CreationError(message) from exc
        if not isinstance(exc, Exception):
            raise
        raise CreationError(message) from exc
    finally:
        _close_transaction_descriptors(parent_descriptors, created_directories)


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_skill(
    args: argparse.Namespace, output: Path, name: str, description: str
) -> CreationPlan:
    skill_root = output / name
    targets = [skill_root / "SKILL.md", skill_root / "agents" / "openai.yaml"]
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
    return CreationPlan(
        files=(
            _planned_text(targets[0], skill),
            _planned_text(targets[1], metadata),
        )
    )


def create_plugin(
    args: argparse.Namespace, output: Path, name: str, description: str
) -> CreationPlan:
    if SEMVER_PATTERN.fullmatch(args.version) is None:
        fail("--version must be strict SemVer")
    author = validate_required_line(args.author, "--author")
    license_name = validate_required_line(args.license, "--license")
    category = validate_required_line(args.category, "--category")
    plugin_root = output / name
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
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
    return CreationPlan(
        files=(_planned_json(manifest_path, manifest),),
        directories=(plugin_root / "skills",),
    )


def validate_plugin_path(raw_path: str) -> str:
    if not raw_path.startswith("./"):
        fail("--plugin-path must start with ./")
    if "\\" in raw_path:
        fail("--plugin-path must use forward slashes")
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".."} for part in pure.parts):
        fail("--plugin-path must stay inside the marketplace root")
    return raw_path.rstrip("/")


def _marketplace_source(args: argparse.Namespace, plugin_name: str) -> dict[str, str]:
    source_type = args.source_type
    if source_type == "local":
        path = validate_plugin_path(args.plugin_path or f"./plugins/{plugin_name}")
        return {"source": "local", "path": path}
    if source_type == "url":
        if not args.source_url:
            fail("--source-type url requires --source-url")
        source: dict[str, str] = {"source": "url", "url": args.source_url}
        if args.source_subdir:
            source["path"] = args.source_subdir
        if args.source_ref:
            source["ref"] = args.source_ref
        return source
    if source_type == "git-subdir":
        if not args.source_url or not args.source_subdir:
            fail("--source-type git-subdir requires --source-url and --source-subdir")
        source = {"source": "git-subdir", "url": args.source_url, "path": args.source_subdir}
        if args.source_ref:
            source["ref"] = args.source_ref
        return source
    if source_type == "npm":
        if not args.npm_package:
            fail("--source-type npm requires --npm-package")
        source = {"source": "npm", "package": args.npm_package}
        if args.npm_version:
            source["version"] = args.npm_version
        if args.npm_registry:
            if not args.npm_registry.startswith("https://"):
                fail("--npm-registry must be an https:// URL")
            source["registry"] = args.npm_registry
        return source
    fail(f"unknown --source-type `{source_type}`")


def create_marketplace(
    args: argparse.Namespace, output: Path, name: str, description: str
) -> CreationPlan:
    if not args.plugin_name:
        fail("marketplace creation requires --plugin-name")
    plugin_name = validate_name(args.plugin_name, "--plugin-name")
    category = validate_required_line(args.category, "--category")
    manifest_path = output / ".agents" / "plugins" / "marketplace.json"
    marketplace = {
        "name": name,
        "interface": {"displayName": display_name(name)},
        "plugins": [
            {
                "name": plugin_name,
                "source": _marketplace_source(args, plugin_name),
                "policy": {
                    "installation": args.install_policy,
                    "authentication": args.auth_policy,
                },
                "category": category,
            }
        ],
    }
    return CreationPlan(files=(_planned_json(manifest_path, marketplace),))


def create_agent(
    args: argparse.Namespace, output: Path, name: str, description: str
) -> CreationPlan:
    target = output / f"{name.replace('_', '-')}.toml"
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
    return CreationPlan(files=(_planned_text(target, content),))


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


def create_hook(
    args: argparse.Namespace, output: Path, name: str, description: str
) -> CreationPlan:
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
    return CreationPlan(files=(_planned_json(target, payload),))


def create_mcp(args: argparse.Namespace, output: Path, name: str, description: str) -> CreationPlan:
    target = output / ".mcp.json"
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
    return CreationPlan(files=(_planned_json(target, {name: server}),))


def create_app(args: argparse.Namespace, output: Path, name: str, description: str) -> CreationPlan:
    if not args.app_id or re.fullmatch(r"plugin_asdk_app_[A-Za-z0-9]+", args.app_id) is None:
        fail("app creation requires a valid --app-id beginning with plugin_asdk_app_")
    category = validate_required_line(args.category, "--category")
    target = output / ".app.json"
    return CreationPlan(
        files=(_planned_json(target, {"apps": {name: {"id": args.app_id, "category": category}}}),)
    )


def create_config(
    args: argparse.Namespace, output: Path, name: str, description: str
) -> CreationPlan:
    target = output / "config.toml"
    default_permissions, approval_policy = PERMISSION_PROFILES[args.permission_profile]
    content = (
        f"default_permissions = {toml_string(default_permissions)}\n"
        f"approval_policy = {toml_string(approval_policy)}\n"
        "\n[features]\n"
        "hooks = true\n"
        "multi_agent = true\n"
    )
    return CreationPlan(files=(_planned_text(target, content),))


def create_instructions(
    args: argparse.Namespace, output: Path, name: str, description: str
) -> CreationPlan:
    target = output / "AGENTS.md"
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
    return CreationPlan(files=(_planned_text(target, content),))


def create_rule(
    args: argparse.Namespace, output: Path, name: str, description: str
) -> CreationPlan:
    if not args.prefix:
        fail("rule creation requires at least one --prefix token")
    if any(
        not token or any(character.isspace() or not character.isprintable() for character in token)
        for token in args.prefix
    ):
        fail("--prefix tokens must be non-empty printable strings without whitespace")
    target = output / f"{name}.rules"
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
    return CreationPlan(files=(_planned_text(target, content),))


def create_requirements(
    args: argparse.Namespace, output: Path, name: str, description: str
) -> CreationPlan:
    target = output / "requirements.toml"
    selected = list(args.allowed_permission_profile)
    for profile in selected:
        if not profile or any(character.isspace() for character in profile):
            fail("--allowed-permission-profile values must be non-empty and whitespace-free")
    default = args.default_permissions
    if default is not None and (not default or any(character.isspace() for character in default)):
        fail("--default-permissions must be a non-empty value without whitespace")
    if not selected:
        # A managed layer with no explicit default must permit both :read-only and
        # :workspace so a lower layer always has a valid default to fall back to.
        selected = [":read-only", ":workspace"]
    if default is not None and default not in selected:
        selected.append(default)
    lines: list[str] = []
    if default is not None:
        lines.append(f"default_permissions = {toml_string(default)}")
        lines.append("")
    lines.append("[allowed_permission_profiles]")
    for profile in selected:
        lines.append(f"{toml_string(profile)} = true")
    content = "\n".join(lines) + "\n"
    return CreationPlan(files=(_planned_text(target, content),))


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
    "requirements": create_requirements,
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
        "--source-type", choices=("local", "url", "git-subdir", "npm"), default="local"
    )
    parser.add_argument("--source-url")
    parser.add_argument("--source-subdir")
    parser.add_argument("--source-ref")
    parser.add_argument("--npm-package")
    parser.add_argument("--npm-version")
    parser.add_argument("--npm-registry")
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
    parser.add_argument("--allowed-permission-profile", action="append", default=[])
    parser.add_argument("--default-permissions")
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
        plan = CREATORS[args.kind](args, output, name, description)
        paths = _apply_plan(plan, force=args.force)
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
