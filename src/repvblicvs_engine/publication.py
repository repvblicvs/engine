"""Check the exact public source/index or release packages without exposing values."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
from .privacy import scan_public_export

PRIVATE_PARTS = {".private", ".claude", ".codex", ".runtime", "coordination", "inbox", "outbox", "logs"}
PRIVATE_NAMES = ("session-retrospective", "team-direction", "handoff-meta", "merchant-readiness", "receiving-wallet", "wallet.encrypted", "acquisition-conversion-results")


def forbidden_path(name: str) -> bool:
    path = Path(name.replace("\\", "/"))
    return (path.is_absolute() or ".." in path.parts or
            any(part.lower() in PRIVATE_PARTS for part in path.parts) or
            path.name.lower() == ".env" or path.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".pem", ".key"} or
            any(item in path.name.lower() for item in PRIVATE_NAMES))


def check_git(root: Path, *, staged: bool = False, revision: str | None = None) -> dict:
    root = Path(root)
    command = ["git", "ls-tree", "-r", "-z", revision] if revision else ["git", "ls-files", "--stage", "-z"]
    raw = subprocess.run(command, cwd=root, check=True, capture_output=True).stdout
    findings, count = [], 0
    with tempfile.TemporaryDirectory(prefix="public-export-") as directory:
        temporary = Path(directory)
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            metadata, path_bytes = entry.split(b"\t", 1)
            if revision:
                mode, object_type, object_id = metadata.decode("ascii").split()
                stage = "0" if object_type == "blob" else "unsupported"
            else:
                mode, object_id, stage = metadata.decode("ascii").split()
            name = path_bytes.decode("utf-8")
            if forbidden_path(name) or mode != "100644" and mode != "100755" or stage != "0":
                findings.append({"file": "[blocked path]", "classification": "private_or_unsupported_source_path"})
                continue
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if staged or revision:
                data = subprocess.run(["git", "cat-file", "blob", object_id], cwd=root, check=True, capture_output=True).stdout
                target.write_bytes(data)
            else:
                source = root / name
                if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(root.resolve()):
                    findings.append({"file": name, "classification": "missing_or_unsafe_source"})
                    continue
                target.write_bytes(source.read_bytes())
            count += 1
        report = scan_public_export(temporary)
        findings.extend(report["findings"])
    return {"allowed": not findings, "findings": findings, "files_checked": count, "mode": "revision" if revision else "index" if staged else "tracked_tree"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--tracked", action="store_true")
    parser.add_argument("--root", default=".")
    parser.add_argument("--revision")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    if args.paths and (args.staged or args.tracked or args.revision) or args.revision and (args.staged or args.tracked):
        parser.error("Choose a Git source check or explicit package paths")
    report = scan_public_export(args.paths) if args.paths else check_git(Path(args.root), staged=args.staged, revision=args.revision)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["allowed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
