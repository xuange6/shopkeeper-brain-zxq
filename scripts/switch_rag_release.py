"""Safely switch between the stage-2 candidate and ACL-compatible rollback index."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "config/releases/stage2-industrial-rag-v2.json"
DEFAULT_ENV = ROOT / "knowledge/.env"
RELEASE_KEYS = (
    "CHUNKS_COLLECTION",
    "INDEX_VERSION",
    "ENTITY_NAME_COLLECTION",
    "KG_GRAPH_VERSION",
)
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.-]+$")


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "rag-release-v1":
        raise ValueError("unsupported release manifest schema")
    for target in ("candidate", "rollback"):
        values = payload.get(target)
        if not isinstance(values, dict):
            raise ValueError(f"manifest target is missing: {target}")
        missing = [key for key in RELEASE_KEYS if not values.get(key)]
        if missing:
            raise ValueError(f"manifest target {target} is missing keys: {missing}")
        invalid = [
            key for key in RELEASE_KEYS
            if not SAFE_VALUE.fullmatch(str(values[key]))
        ]
        if invalid:
            raise ValueError(f"manifest target {target} has unsafe values: {invalid}")
    return payload


def current_release_values(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    counts: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if not match or match.group(1) not in RELEASE_KEYS:
            continue
        key = match.group(1)
        counts[key] = counts.get(key, 0) + 1
        result[key] = match.group(2)
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate release keys in env file: {duplicates}")
    return result


def render_env(text: str, values: dict[str, str]) -> str:
    current_release_values(text)
    lines = text.splitlines()
    replaced: set[str] = set()
    rendered: list[str] = []
    for line in lines:
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        key = match.group(1) if match else ""
        if key in values:
            rendered.append(f"{key}={values[key]}")
            replaced.add(key)
        else:
            rendered.append(line)
    if rendered and rendered[-1] != "":
        rendered.append("")
    for key in RELEASE_KEYS:
        if key not in replaced:
            rendered.append(f"{key}={values[key]}")
    return "\n".join(rendered).rstrip() + "\n"


def switch_release(
    env_path: Path,
    manifest_path: Path,
    target: str,
    *,
    apply: bool,
    force: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    if target not in {"candidate", "rollback"}:
        raise ValueError(f"unsupported release target: {target}")
    if not env_path.is_file():
        raise FileNotFoundError(f"environment file does not exist: {env_path}")

    text = env_path.read_text(encoding="utf-8")
    current = current_release_values(text)
    known_targets = [manifest["candidate"], manifest["rollback"]]
    if not force and current and not any(
        all(current.get(key) == values[key] for key in RELEASE_KEYS)
        for values in known_targets
    ):
        raise ValueError(
            "current release values do not match candidate or rollback; "
            "inspect the environment or pass --force"
        )

    desired = {key: str(manifest[target][key]) for key in RELEASE_KEYS}
    report: dict[str, Any] = {
        "schema_version": "rag-release-switch-v1",
        "release_id": manifest["release_id"],
        "target": target,
        "mode": "apply" if apply else "dry-run",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "before": {key: current.get(key, "") for key in RELEASE_KEYS},
        "after": desired,
        "changed_keys": [
            key for key in RELEASE_KEYS if current.get(key) != desired[key]
        ],
        "backup": "",
    }
    if not apply:
        return report

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = env_path.with_name(f"{env_path.name}.bak.{timestamp}")
    if backup.exists():
        raise FileExistsError(f"backup already exists: {backup}")
    shutil.copy2(env_path, backup)
    rendered = render_env(text, desired)
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=env_path.parent,
        prefix=env_path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, env_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    report["backup"] = str(backup)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("candidate", "rollback"), required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--apply", action="store_true", help="Apply after the default dry-run.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow switching an environment that does not match either manifest target.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = switch_release(
            args.env_file.resolve(),
            args.manifest.resolve(),
            args.target,
            apply=args.apply,
            force=args.force,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"release switch refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.apply:
        print("Run scripts/verify_stage2_index.py before accepting traffic.")
    else:
        print("Dry run only. Repeat with --apply to change the environment file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
