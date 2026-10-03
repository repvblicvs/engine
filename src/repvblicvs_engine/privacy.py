"""Conservative public-export checks that report locations, never secret values."""

from __future__ import annotations

import io
import re
import tarfile
import zipfile
from pathlib import Path
from typing import Iterable


PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "credential": re.compile(r"(?<![\w-])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk_(?:live|test)_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{20,})(?![\w-])"),
    "assigned_secret": re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\b\s*[:=]\s*['\"]([A-Za-z0-9_+./=-]{12,})['\"]"),
    "email": re.compile(r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}(?![\w.-])"),
    "private_path": re.compile(r"(?:/Us" + r"ers/[^\s/\"'<>]+/|/ho" + r"me/[^\s/\"'<>]+/|[A-Za-z]:\\Us" + r"ers\\[^\s\\\"'<>]+\\)"),
    "customer_material": re.compile(r"(?i)\b(?:CUSTOMER_" + r"CONFIDENTIAL|PRIVATE_" + r"CUSTOMER_DATA|BEGIN PRIVATE " + r"CUSTOMER RECORD)\b"),
}
PLACEHOLDERS = {"your_api_key_here", "example_password", "replace_me_token", "changeme_password"}


def scan_public_export(paths: Path | str | Iterable[Path | str], *, allowed_emails: Iterable[str] = (), private_roots: Iterable[str] = (), max_bytes: int = 50 * 1024 * 1024, max_files: int = 5000) -> dict:
    """Inspect files and ZIP/TAR members without extracting or printing contents.

    Symlinks, malformed/unreadable archives, traversal names, binary files, and
    size-limit failures are blocked rather than interpreted as a clean audit.
    This supplements source review; it cannot identify all private information.
    """
    if isinstance(paths, (Path, str)): paths = [paths]
    allowed = {value.lower() for value in allowed_emails}
    roots = [str(value) for value in private_roots if value]
    findings, count, total = [], 0, 0

    def add(name: str, classification: str, line: int | None = None) -> None:
        if PATTERNS["email"].search(name) or PATTERNS["credential"].search(name):
            name = "[redacted filename]"
        finding = {"file": name, "classification": classification}
        if line is not None: finding["line"] = line
        if finding not in findings: findings.append(finding)

    def inspect(name: str, data: bytes, depth: int = 0) -> None:
        nonlocal count, total
        count += 1
        total += len(data)
        if count > max_files or total > max_bytes:
            add(name, "scan_limit_exceeded"); return
        # Embedded archives can bypass review; inspect bounded nesting in memory.
        suffix = Path(name).suffix.lower()
        is_zip = suffix in {".zip", ".whl"} or data.startswith(b"PK\x03\x04")
        is_tar = suffix in {".tar", ".tgz", ".gz", ".bz2", ".xz"}
        if is_zip or is_tar:
            if depth >= 2: add(name, "archive_nesting_limit"); return
            try:
                if is_zip:
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        for member in archive.infolist():
                            member_name = name + "!" + member.filename
                            if member.is_dir(): continue
                            mode = (member.external_attr >> 16) & 0o170000
                            if member.filename.startswith(("/", "\\")) or ".." in Path(member.filename.replace("\\", "/")).parts: add(member_name, "archive_unsafe_path"); continue
                            if mode == 0o120000: add(member_name, "symlink_not_audited"); continue
                            if member.flag_bits & 1: add(member_name, "encrypted_archive_member"); continue
                            if member.file_size > max_bytes - total: add(member_name, "scan_limit_exceeded"); continue
                            inspect(member_name, archive.read(member), depth + 1)
                else:
                    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
                        for member in archive:
                            member_name = name + "!" + member.name
                            if member.isdir(): continue
                            if not member.isfile(): add(member_name, "archive_special_file"); continue
                            if member.name.startswith(("/", "\\")) or ".." in Path(member.name.replace("\\", "/")).parts: add(member_name, "archive_unsafe_path"); continue
                            if member.size > max_bytes - total: add(member_name, "scan_limit_exceeded"); continue
                            stream = archive.extractfile(member)
                            if stream is None: add(member_name, "archive_unreadable_member"); continue
                            inspect(member_name, stream.read(), depth + 1)
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError): add(name, "archive_unreadable")
            return
        try:
            text = data.decode("utf-8")
            if "\x00" in text: raise UnicodeError("binary")
        except UnicodeError:
            add(name, "binary_requires_manual_review"); return
        for classification, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                if classification == "email" and match[0].lower() in allowed: continue
                if classification == "assigned_secret" and match[1].lower() in PLACEHOLDERS: continue
                add(name, classification, text.count("\n", 0, match.start()) + 1)
        for root in roots:
            if root in text: add(name, "private_root", text.count("\n", 0, text.index(root)) + 1)
        # Sensitive filenames are also an export concern; values remain redacted.
        if PATTERNS["email"].search(name): add("[redacted filename]", "private_filename")

    for item in paths:
        base = Path(item)
        if not base.exists() and not base.is_symlink():
            add(base.name, "missing_input"); continue
        entries = sorted(base.rglob("*")) if base.is_dir() and not base.is_symlink() else [base]
        for path in entries:
            name = str(path.relative_to(base)) if base.is_dir() else path.name
            if path.is_symlink(): add(name, "symlink_not_audited"); continue
            if not path.is_file(): continue
            try:
                if count >= max_files or total + path.stat().st_size > max_bytes:
                    add(name, "scan_limit_exceeded"); continue
                inspect(name, path.read_bytes())
            except OSError: add(name, "unreadable_file")
    return {"allowed": not findings, "findings": findings, "files_scanned": count, "bytes_scanned": total, "limitations": ["Pattern checks supplement source review; clean output is not proof that all private information was identified."]}
