#!/usr/bin/env python3
"""Transactional setup manager for a caller-selected Codex home."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn


ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = ROOT / "setups"
VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()
PRODUCT_NAME = "nddev-codex-app"
STAMP_NAME = "NDDEV-CODEX-SETUP.json"
BACKUP_NAME = "NDDEV-CODEX-BACKUP.json"
MANAGED_FILES = ("config.toml", "AGENTS.md")
SETUP_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
SEMVER_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
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


def fail(message: str) -> NoReturn:
    raise CodexSetupError(message)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        fail(f"{label} has invalid keys (missing={missing}, extra={extra})")


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    require_regular_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read valid JSON from {label}: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must contain a JSON object")
    return value


def require_regular_file(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular non-symlink file")
    if info.st_nlink != 1:
        fail(f"{label} must not have hard-link aliases")
    return info


def validate_setup_id(setup_id: str) -> None:
    if not SETUP_ID_PATTERN.fullmatch(setup_id):
        fail(f"invalid setup id: {setup_id!r}")


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
        require_regular_file(path, f"setup {setup_id}/{name}")
        try:
            content = path.read_bytes()
            content.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            fail(f"setup {setup_id}/{name} must be valid UTF-8: {exc}")
        if not content or not content.endswith(b"\n") or b"\r" in content:
            fail(f"setup {setup_id}/{name} must be non-empty LF-terminated text")
        rendered[name] = content

    try:
        config = tomllib.loads(rendered["config.toml"].decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        fail(f"setup {setup_id}/config.toml is invalid TOML: {exc}")
    if set(config) != {"default_permissions", "approval_policy"}:
        fail(
            f"setup {setup_id}/config.toml may define only "
            "default_permissions and approval_policy"
        )
    if not isinstance(config["default_permissions"], str):
        fail(f"setup {setup_id}/config.toml default_permissions must be a string")
    if not isinstance(config["approval_policy"], str):
        fail(f"setup {setup_id}/config.toml approval_policy must be a string")
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


def managed_path(target: Path, name: str) -> Path:
    path = target / name
    if path.exists() or path.is_symlink():
        require_regular_file(path, f"managed path {path}")
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
    if not stamp_path.exists() and not stamp_path.is_symlink():
        return None
    stamp = load_json_object(stamp_path, f"managed stamp {stamp_path}")
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
    if not target.exists():
        return {
            "state": "missing",
            "setup_id": None,
            "build_version": None,
            "drift": [],
            "unmanaged_managed_paths": [],
        }

    stamp = load_stamp(target)
    existing = []
    for name in MANAGED_FILES:
        path = managed_path(target, name)
        if path.exists():
            existing.append(name)
    if stamp is None:
        return {
            "state": "unmanaged",
            "setup_id": None,
            "build_version": None,
            "drift": [],
            "unmanaged_managed_paths": existing,
        }

    expected = validate_digest_map(
        stamp["managed_files"], "managed stamp managed_files"
    )
    drift: list[str] = []
    for name in MANAGED_FILES:
        path = managed_path(target, name)
        if (
            expected[name] is None
            or not path.exists()
            or sha256_file(path) != expected[name]
        ):
            drift.append(name)
    return {
        "state": "managed",
        "setup_id": stamp["setup_id"],
        "build_version": stamp["build_version"],
        "drift": drift,
        "unmanaged_managed_paths": [],
    }


def require_clean_managed(target: Path) -> dict[str, Any]:
    status = inspect_target(target)
    if status["state"] != "managed":
        fail("target is not managed by nddev-codex-app")
    if status["drift"]:
        fail(f"managed target has drift: {', '.join(status['drift'])}")
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
def target_lock(target: Path) -> Iterator[None]:
    lock = lock_path(target)
    if lock.exists() or lock.is_symlink():
        fail(f"target is locked: {lock}")
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        fail(f"target is locked: {lock}")
    try:
        owner = canonical_json(
            {"schema_version": 1, "pid": os.getpid(), "target": str(target)}
        )
        write_new_file(lock / "owner.json", owner)
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def ensure_pool(target: Path) -> Path:
    pool = backup_pool(target)
    if pool.exists() or pool.is_symlink():
        info = pool.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            fail(f"backup pool is unsafe: {pool}")
    else:
        pool.mkdir(mode=0o700)
    os.chmod(pool, 0o700)
    return pool


def write_new_file(path: Path, content: bytes, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def choose_backup_slot(pool: Path, target: Path, exclude: int | None = None) -> int:
    candidates: list[tuple[int, int]] = []
    for slot in range(10):
        path = pool / str(slot)
        if not path.exists() and not path.is_symlink():
            return slot
        if path.is_symlink() or not path.is_dir():
            fail(f"backup slot is unsafe: {path}")
        envelope = load_backup_envelope(path, slot, expected_target=target)
        candidates.append((path.stat().st_mtime_ns, envelope["slot"]))
    eligible = [item for item in candidates if item[1] != exclude]
    if not eligible:
        fail("no backup slot is available without destroying the restore source")
    return min(eligible)[1]


def load_backup_envelope(
    slot_path: Path, slot: int, expected_target: Path
) -> dict[str, Any]:
    envelope = load_json_object(slot_path / BACKUP_NAME, f"backup slot {slot}")
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
    payload = slot_path / "payload"
    if not payload.is_dir() or payload.is_symlink():
        fail(f"backup slot {slot} payload is unsafe")
    for name, digest in digests.items():
        path = payload / name
        if digest is None:
            if path.exists() or path.is_symlink():
                fail(f"backup slot {slot} contains undeclared payload {name}")
        else:
            require_regular_file(path, f"backup slot {slot} payload {name}")
            if sha256_file(path) != digest:
                fail(f"backup slot {slot} payload digest mismatch for {name}")
    stamp_path = payload / STAMP_NAME
    if stamp_digest is None:
        if stamp_path.exists() or stamp_path.is_symlink():
            fail(f"backup slot {slot} contains an undeclared managed stamp")
    else:
        require_regular_file(stamp_path, f"backup slot {slot} managed stamp")
        if sha256_file(stamp_path) != stamp_digest:
            fail(f"backup slot {slot} managed stamp digest mismatch")
    allowed_payload = {name for name, digest in digests.items() if digest is not None}
    if stamp_digest is not None:
        allowed_payload.add(STAMP_NAME)
    actual_payload = {path.name for path in payload.iterdir()}
    if actual_payload != allowed_payload:
        fail(f"backup slot {slot} contains unexpected payload entries")
    return envelope


def restore_backup_payload(target: Path, slot: int) -> None:
    slot_path = backup_pool(target) / str(slot)
    if slot_path.is_symlink() or not slot_path.is_dir():
        fail(f"backup slot does not exist: {slot}")
    envelope = load_backup_envelope(slot_path, slot, target)
    restore_managed_files(target, slot_path / "payload", envelope["managed_files"])


def restore_managed_files(
    target: Path, payload: Path, digests: dict[str, str | None]
) -> None:
    target.mkdir(mode=0o700, exist_ok=True)
    for name in (*MANAGED_FILES, STAMP_NAME):
        path = target / name
        if path.exists() or path.is_symlink():
            if name != STAMP_NAME or path.exists():
                require_regular_file(path, f"managed path {path}")
            path.unlink()
    for name in MANAGED_FILES:
        if digests[name] is not None:
            source = payload / name
            write_new_file(target / name, source.read_bytes())
    if digests["config.toml"] is not None and digests["AGENTS.md"] is not None:
        source_stamp = payload / STAMP_NAME
        if source_stamp.exists() or source_stamp.is_symlink():
            require_regular_file(source_stamp, "backup managed stamp")
            write_new_file(target / STAMP_NAME, source_stamp.read_bytes())


def create_transaction_backup(target: Path, exclude: int | None = None) -> int:
    status = inspect_target(target)
    setup_id = status["setup_id"] if status["state"] == "managed" else None
    pool = ensure_pool(target)
    slot = choose_backup_slot(pool, target, exclude=exclude)
    destination = pool / str(slot)
    hold = pool / f".{slot}.replaced"
    if hold.exists() or hold.is_symlink():
        fail(f"backup recovery hold already exists: {hold}")
    if destination.exists():
        os.replace(destination, hold)
    try:
        destination.mkdir(mode=0o700)
        payload = destination / "payload"
        payload.mkdir(mode=0o700)
        digests: dict[str, str | None] = {}
        for name in MANAGED_FILES:
            path = target / name
            if path.exists() or path.is_symlink():
                require_regular_file(path, f"managed path {path}")
                content = path.read_bytes()
                write_new_file(payload / name, content)
                digests[name] = sha256_bytes(content)
            else:
                digests[name] = None
        stamp = target / STAMP_NAME
        stamp_digest: str | None = None
        if stamp.exists() or stamp.is_symlink():
            require_regular_file(stamp, f"managed stamp {stamp}")
            stamp_content = stamp.read_bytes()
            write_new_file(payload / STAMP_NAME, stamp_content)
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
        write_new_file(destination / BACKUP_NAME, canonical_json(envelope))
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        if hold.exists():
            os.replace(hold, destination)
        raise
    if hold.exists():
        shutil.rmtree(hold)
    return slot


def apply_rendered(target: Path, setup_id: str, rendered: dict[str, bytes]) -> None:
    target.mkdir(mode=0o700, exist_ok=True)
    os.chmod(target, 0o700)
    with tempfile.TemporaryDirectory(
        prefix=f".{target.name}.nddev-stage-", dir=target.parent
    ) as raw:
        stage = Path(raw)
        for name in MANAGED_FILES:
            write_new_file(stage / name, rendered[name])
        write_new_file(stage / STAMP_NAME, stamp_bytes(target, setup_id, rendered))
        for name in (*MANAGED_FILES, STAMP_NAME):
            os.replace(stage / name, target / name)


def plan_setup(target: Path, setup_id: str) -> dict[str, Any]:
    _, rendered = render_setup(setup_id)
    status = inspect_target(target)
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
        current_path = target / name
        current = sha256_file(current_path) if current_path.exists() else None
        if current != desired:
            changes.append(name)
    desired_stamp = sha256_bytes(stamp_bytes(target, setup_id, rendered))
    stamp_path = target / STAMP_NAME
    current_stamp = sha256_file(stamp_path) if stamp_path.exists() else None
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

    with target_lock(target):
        plan = plan_setup(target, setup_id)
        backup_slot: int | None = None
        if plan["backup_required"]:
            backup_slot = create_transaction_backup(target)
        try:
            apply_rendered(target, setup_id, rendered)
            final = require_clean_managed(target)
            if final["setup_id"] != setup_id:
                fail("postcondition failed: setup identity mismatch")
        except BaseException:
            if backup_slot is not None:
                restore_backup_payload(target, backup_slot)
            else:
                for name in (*MANAGED_FILES, STAMP_NAME):
                    path = target / name
                    if path.exists() and path.is_file() and not path.is_symlink():
                        path.unlink()
            raise
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
    if status["state"] == "unmanaged" and status["unmanaged_managed_paths"]:
        fail("cannot restore over unmanaged managed paths")
    if status["state"] == "managed" and status["drift"]:
        fail(f"managed target has drift: {', '.join(status['drift'])}")
    source = backup_pool(target) / str(slot)
    if source.is_symlink() or not source.is_dir():
        fail(f"backup slot does not exist: {slot}")
    envelope = load_backup_envelope(source, slot, target)
    if envelope["source_setup_id"] is None:
        fail("selected backup does not contain a managed Codex setup")

    with target_lock(target):
        rollback_slot = create_transaction_backup(target, exclude=slot)
        try:
            restore_backup_payload(target, slot)
            require_clean_managed(target)
        except BaseException:
            restore_backup_payload(target, rollback_slot)
            raise
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
    with target_lock(target):
        status = require_clean_managed(target)
        backup_slot = create_transaction_backup(target)
        try:
            for name in (*MANAGED_FILES, STAMP_NAME):
                path = target / name
                require_regular_file(path, f"managed path {path}")
                path.unlink()
        except BaseException:
            restore_backup_payload(target, backup_slot)
            raise
    return {
        "schema_version": 1,
        "command": "remove",
        "target": str(target),
        "removed_setup_id": status["setup_id"],
        "backup_slot": backup_slot,
    }


def human_output(value: dict[str, Any]) -> str:
    command = value.get("command")
    if command == "list":
        return "\n".join(
            f"{item['id']}: {item['description']}" for item in value["setups"]
        )
    if command == "status":
        setup = f" ({value['setup_id']})" if value["setup_id"] else ""
        drift = f"; drift={','.join(value['drift'])}" if value["drift"] else ""
        return f"{value['state']}{setup}: {value['target']}{drift}"
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
    return parser


def add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True, help="Absolute Codex home path.")
    parser.add_argument("--json", action="store_true", dest="output_json")


def run(args: argparse.Namespace) -> dict[str, Any]:
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
    fail(f"unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except CodexSetupError as exc:
        if getattr(args, "output_json", False):
            print(json.dumps({"schema_version": 1, "error": str(exc)}, sort_keys=True))
        else:
            print(f"nddev-codex: error: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "output_json", False):
        sys.stdout.buffer.write(canonical_json(result))
    else:
        print(human_output(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
