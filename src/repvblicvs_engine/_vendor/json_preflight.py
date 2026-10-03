#!/usr/bin/env python3
"""Read-only JSON syntax and duplicate-object-key preflight."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

MAX_BYTES = 1024 * 1024
MAX_NESTING = 64
MAX_FINDINGS = 100
MAX_POINTER_CHARS = 512
MAX_REPORT_BYTES = 512 * 1024


class ObjectPairs:
    """Preserve object members so duplicate names are not discarded."""

    def __init__(self, pairs):
        self.pairs = pairs


class NonStandardConstant(ValueError):
    pass


class PreflightError(Exception):
    def __init__(self, code, message, *, line=None, column=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line
        self.column = column


def _nesting_error(text: str) -> bool:
    """Check structural depth outside strings before invoking the JSON parser."""
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_NESTING:
                return True
        elif char in "]}":
            depth = max(0, depth - 1)
    return False


def _pointer(path: str, key: str) -> tuple[str, bool]:
    full = path + "/" + key.replace("~", "~0").replace("/", "~1")
    if len(full) <= MAX_POINTER_CHARS:
        return full, False
    return full[:MAX_POINTER_CHARS], True


def inspect_bytes(data: bytes) -> dict:
    """Return a bounded diagnostic report without coercing or exposing values."""
    report = {
        "tool": "repvblicvs-json-preflight",
        "version": 1,
        "status": "refused",
        "input_bytes": len(data),
        "limits": {"max_bytes": MAX_BYTES, "max_nesting": MAX_NESTING},
        "duplicate_count": 0,
        "duplicates": [],
        "duplicates_omitted": 0,
        "error": None,
    }

    def refuse(code, message, *, line=None, column=None):
        report["error"] = {"code": code, "message": message}
        if line is not None:
            report["error"]["line"] = line
            report["error"]["column"] = column
        return report

    if len(data) > MAX_BYTES:
        return refuse("size_limit", "Input exceeds the 1 MiB byte limit.")
    if data.startswith(b"\xef\xbb\xbf"):
        return refuse("utf8_bom", "A UTF-8 BOM is not accepted.")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return refuse("invalid_utf8", "Input is not valid UTF-8.")
    if _nesting_error(text):
        return refuse("nesting_limit", "Input exceeds the 64-level nesting limit.")
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=ObjectPairs,
            parse_int=lambda number: number,
            parse_float=lambda number: number,
            parse_constant=lambda _value: (_ for _ in ()).throw(NonStandardConstant()),
        )
    except NonStandardConstant:
        return refuse("nonstandard_number", "NaN and Infinity are not valid JSON numbers.")
    except json.JSONDecodeError as error:
        return refuse(
            "invalid_json", "Input is not syntactically valid JSON.",
            line=error.lineno, column=error.colno,
        )
    except RecursionError:
        # Defensive guard: nesting is bounded before parsing; never alter interpreter limits.
        return refuse("nesting_limit", "Input exceeds the supported nesting limit.")

    def visit(node, path):
        if isinstance(node, ObjectPairs):
            seen = set()
            for key, value in node.pairs:
                if key in seen:
                    report["duplicate_count"] += 1
                    if len(report["duplicates"]) < MAX_FINDINGS:
                        location, truncated = _pointer(path, key)
                        report["duplicates"].append({
                            "pointer": location,
                            "pointer_truncated": truncated,
                        })
                    else:
                        report["duplicates_omitted"] += 1
                seen.add(key)
                visit(value, path + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, path + "/" + str(index))

    visit(parsed, "")
    if report["duplicate_count"] == 0:
        report["status"] = "ok"
    return report


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise PreflightError("command_error", "Invalid command arguments.")

    def print_help(self, file=None):
        # Keep argparse from writing non-JSON text before the structured report.
        return None

    def exit(self, status=0, message=None):
        if status == 0:
            raise PreflightError("help_requested", "Help requested; run with an input file.")
        raise PreflightError("command_error", "Could not parse command arguments.")


def _emit(report):
    encoded = json.dumps(report, ensure_ascii=True, separators=(",", ":")) + "\n"
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        # This should be unreachable with the finding and pointer caps.
        encoded = json.dumps({
            "tool": "repvblicvs-json-preflight", "version": 1,
            "status": "error", "error": {"code": "report_limit",
            "message": "Diagnostic report exceeded its output limit."},
        }, separators=(",", ":")) + "\n"
    sys.stdout.write(encoded)


def main(argv=None) -> int:
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON input file")
    try:
        args = parser.parse_args(argv)
    except PreflightError as error:
        if error.code == "help_requested":
            _emit({
                "tool": "repvblicvs-json-preflight", "version": 1,
                "status": "help",
                "message": "Usage: python3 tools/json_preflight.py INPUT.json",
            })
            return 0
        _emit({
            "tool": "repvblicvs-json-preflight", "version": 1,
            "status": "error",
            "error": {"code": error.code, "message": error.message},
        })
        return 2

    try:
        with args.input.open("rb") as source:
            data = source.read(MAX_BYTES + 1)
    except (OSError, ValueError) as error:
        _emit({
            "tool": "repvblicvs-json-preflight", "version": 1,
            "status": "error",
            "error": {
                "code": "file_error",
                "message": f"Could not read input file ({type(error).__name__}).",
            },
        })
        return 2

    report = inspect_bytes(data)
    _emit(report)
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
