"""Verify the imported source snapshot without mutating either project tree."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path, PurePosixPath


PLAN_DOCUMENT = "EviForge_项目修改方案.md"
EXCLUDED_PARTS = frozenset({".pnpm-store", "__pycache__"})
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})


def source_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if EXCLUDED_PARTS.intersection(relative.parts):
            continue
        if path.suffix.casefold() in EXCLUDED_SUFFIXES:
            continue
        files[relative.as_posix()] = path
    return files


def raw_import_files(repo: Path, revision: str) -> dict[str, bytes]:
    completed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", revision],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    names = completed.stdout.decode("utf-8").rstrip("\0").split("\0")
    result: dict[str, bytes] = {}
    for name in names:
        if not name or name == PLAN_DOCUMENT:
            continue
        relative = PurePosixPath(name)
        if EXCLUDED_PARTS.intersection(relative.parts):
            continue
        if relative.suffix.casefold() in EXCLUDED_SUFFIXES:
            continue
        blob = subprocess.run(
            ["git", "show", f"{revision}:{name}"],
            cwd=repo,
            check=True,
            capture_output=True,
        ).stdout
        result[name] = blob
    return result


def watermark_counts(root: Path) -> dict[str, int]:
    markers = (
        "来源：" + "公众号@" + "小林" + "coding",
        "后端八股网站：" + "xiaolin" + "coding.com",
        "Agent网站：" + "xiaolin" + "note.com",
        "简历模版：" + "jianli." + "xiaolin" + "note.com",
    )
    counts = {marker: 0 for marker in markers}
    for path in source_files(root).values():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for marker in markers:
            counts[marker] += text.count(marker)
    return counts


def manifest_rows(files: dict[str, bytes]) -> list[str]:
    return [
        f"{name}\t{hashlib.sha256(files[name]).hexdigest()}"
        for name in sorted(files, key=lambda value: (value.casefold(), value))
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--revision", default="raw-import")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="write the canonical source manifest before verifying it",
    )
    args = parser.parse_args()

    source_root = args.source.expanduser().resolve(strict=True)
    repo = args.repo.expanduser().resolve(strict=True)
    local_paths = source_files(source_root)
    source_blobs = {name: path.read_bytes() for name, path in local_paths.items()}
    imported_blobs = raw_import_files(repo, args.revision)
    missing = sorted(set(source_blobs) - set(imported_blobs))
    extra = sorted(set(imported_blobs) - set(source_blobs))
    mismatched = sorted(
        name
        for name in set(source_blobs) & set(imported_blobs)
        if source_blobs[name] != imported_blobs[name]
    )
    rows = manifest_rows(source_blobs)
    manifest_bytes = "\n".join(rows).encode("utf-8")
    aggregate = hashlib.sha256(manifest_bytes).hexdigest()

    if args.manifest is not None:
        if args.write_manifest:
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            args.manifest.write_bytes(manifest_bytes)
        expected = args.manifest.read_bytes()
        if expected != manifest_bytes:
            print("source manifest does not match current source tree", file=sys.stderr)
            return 1

    print(f"source_files={len(source_blobs)}")
    print(f"source_bytes={sum(len(value) for value in source_blobs.values())}")
    print(f"raw_import_files={len(imported_blobs)}")
    print(f"missing={len(missing)} extra={len(extra)} mismatched={len(mismatched)}")
    print(f"aggregate_sha256={aggregate}")
    watermarks = watermark_counts(source_root)
    print("watermark_counts=" + ",".join(str(value) for value in watermarks.values()))
    if missing or extra or mismatched or any(watermarks.values()):
        for label, values in (("missing", missing), ("extra", extra), ("mismatched", mismatched)):
            for value in values:
                print(f"{label}: {value}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
