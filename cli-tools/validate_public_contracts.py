#!/usr/bin/env python3
"""Validate the public NDDev Codex module contracts without private inputs.

This is the repository-owned fast verification entry point declared in
`.gds/repository.yaml`. It checks only tracked public contract files and
never reads private harness material, user state, or the network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:  # Python 3.11+; python_requires stays >=3.10 for the setup manager.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - explicit degraded mode
    tomllib = None

REQUIRED_VERSION_KEYS = (
    "build_version",
    "codex_cli_tested",
    "codex_permission_profiles_since",
    "nddev_builder_plugin_version",
    "python_requires",
    "runtime_baseline_ref",
    "schema_version",
)


def load_json(relative: str, errors: list[str]) -> dict | None:
    path = ROOT / relative
    if not path.is_file():
        errors.append(f"missing required contract file: {relative}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{relative}: unreadable or invalid JSON: {exc}")
        return None
    if not isinstance(data, dict):
        errors.append(f"{relative}: top-level value must be an object")
        return None
    return data


def resolve_version_ref(ref: str, version: dict, errors: list[str]) -> None:
    prefix = "build/version.json:"
    if not ref.startswith(prefix):
        errors.append(f"unsupported version ref format: {ref}")
        return
    key = ref[len(prefix) :]
    if key not in version:
        errors.append(f"version ref target missing in build/version.json: {key}")


def check_toml(relative: str, errors: list[str], notices: list[str]) -> None:
    path = ROOT / relative
    if not path.is_file():
        errors.append(f"missing managed template: {relative}")
        return
    try:
        raw = path.read_bytes()
        raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{relative}: unreadable managed template: {exc}")
        return
    if not raw.strip():
        errors.append(f"{relative}: managed template is empty")
        return
    if tomllib is None:
        notices.append(f"{relative}: TOML deep parse skipped (Python < 3.11)")
        return
    try:
        tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        errors.append(f"{relative}: invalid TOML: {exc}")


def main() -> int:
    errors: list[str] = []
    notices: list[str] = []

    version = load_json("build/version.json", errors)
    manifest = load_json("build/manifest.json", errors)
    contract = load_json("config/nddev-contract.json", errors)
    baseline = load_json("references/codex-baseline.json", errors)
    plugin = load_json("plugins/nddev-builder/.codex-plugin/plugin.json", errors)
    marketplace = load_json(".agents/plugins/marketplace.json", errors)

    version_file = ROOT / "VERSION"
    if not version_file.is_file():
        errors.append("missing VERSION file")
        declared = None
    else:
        declared = version_file.read_text(encoding="utf-8").strip()

    if version is not None:
        for key in REQUIRED_VERSION_KEYS:
            if key not in version:
                errors.append(f"build/version.json: missing required key {key}")
        if declared is not None and version.get("build_version") != declared:
            errors.append(
                "VERSION and build/version.json:build_version disagree: "
                f"{declared!r} != {version.get('build_version')!r}"
            )

    if manifest is not None and version is not None:
        if manifest.get("build_version") != version.get("build_version"):
            errors.append(
                "build/manifest.json:build_version disagrees with build/version.json"
            )

    if contract is not None:
        if contract.get("contract_version") != 3:
            errors.append("config/nddev-contract.json: contract_version must be 3")
        if contract.get("github_repository") != "NDDev-it-com/nddev-codex-app":
            errors.append("config/nddev-contract.json: unexpected github_repository")
        manifest_ref = contract.get("manifest_ref")
        if manifest_ref and not (ROOT / str(manifest_ref)).is_file():
            errors.append(f"config/nddev-contract.json: manifest_ref missing: {manifest_ref}")
        managed = contract.get("managed_state", {}).get("managed_files")
        if not managed:
            errors.append("config/nddev-contract.json: managed_state.managed_files empty")

    if baseline is not None and version is not None:
        tested_ref = baseline.get("codex_cli", {}).get("tested_version_ref", "")
        resolve_version_ref(str(tested_ref), version, errors)
        minimum_ref = baseline.get("permission_profiles", {}).get(
            "minimum_codex_version_ref", ""
        )
        resolve_version_ref(str(minimum_ref), version, errors)

    setups_root = ROOT / "setups"
    setup_dirs = sorted(p for p in setups_root.iterdir() if p.is_dir()) if setups_root.is_dir() else []
    if not setup_dirs:
        errors.append("setups/: no setup projections found")
    seen_ids: list[str] = []
    for setup_dir in setup_dirs:
        relative = setup_dir.relative_to(ROOT).as_posix()
        setup = load_json(f"{relative}/setup.json", errors)
        if setup is not None:
            setup_id = setup.get("id")
            if setup_id != setup_dir.name:
                errors.append(f"{relative}/setup.json: id must equal directory name")
            else:
                seen_ids.append(str(setup_id))
            for managed_name in setup.get("managed_files", []):
                if not (setup_dir / str(managed_name)).is_file():
                    errors.append(f"{relative}: declared managed file missing: {managed_name}")
        check_toml(f"{relative}/config.toml", errors, notices)
        agents_doc = setup_dir / "AGENTS.md"
        if not agents_doc.is_file() or not agents_doc.read_text(encoding="utf-8").strip():
            errors.append(f"{relative}/AGENTS.md: missing or empty")

    if manifest is not None:
        declared_ids = manifest.get("setup_ids")
        if isinstance(declared_ids, list) and sorted(declared_ids) != seen_ids:
            errors.append(
                "build/manifest.json:setup_ids disagrees with setups/ directories: "
                f"{sorted(declared_ids)} != {seen_ids}"
            )

    if plugin is not None and version is not None:
        if plugin.get("name") != "nddev-builder":
            errors.append("plugin manifest: name must be nddev-builder")
        if plugin.get("version") != version.get("nddev_builder_plugin_version"):
            errors.append(
                "plugin manifest version disagrees with "
                "build/version.json:nddev_builder_plugin_version"
            )

    if marketplace is not None:
        entries = marketplace.get("plugins", [])
        paths = [e.get("source", {}).get("path") for e in entries if isinstance(e, dict)]
        if "./plugins/nddev-builder" not in paths:
            errors.append(".agents/plugins/marketplace.json: nddev-builder source path missing")
        for entry_path in paths:
            if entry_path and not (ROOT / str(entry_path)).is_dir():
                errors.append(f"marketplace source path does not exist: {entry_path}")

    for notice in notices:
        print(f"validate_public_contracts.py: NOTE {notice}")
    if errors:
        print(f"validate_public_contracts.py: FAIL ({len(errors)} error(s))")
        for item in errors:
            print(f"  - {item}")
        return 1
    print("validate_public_contracts.py: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
