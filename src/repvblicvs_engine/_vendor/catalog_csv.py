#!/usr/bin/env python3
"""Local CSV triage and value-preserving formatting; no store access."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 100_000
SOURCE = "https://help.shopify.com/en/manual/products/import-export/using-csv"
HEADERS = {
    "current": {
        "handle": "URL handle", "sku": "SKU", "price": "Price",
        "compare": "Compare-at price", "option_name": "Option1 name",
        "option_value": "Option1 value", "weight": "Weight value (grams)",
    },
    "legacy": {
        "handle": "Handle", "sku": "Variant SKU", "price": "Variant Price",
        "compare": "Variant Compare-at Price", "option_name": "Option1 Name",
        "option_value": "Option1 Value", "weight": "Variant Grams",
    },
}
PRICE = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z", re.ASCII)


def _quote_error(text: str) -> str | None:
    """Reject quotes that csv.reader accepts inside an unquoted field."""
    state = "start"
    for char in text:
        if state == "quoted":
            if char == '"':
                state = "closed"
        elif state == "closed":
            if char == '"':
                state = "quoted"
            elif char in ",\r\n":
                state = "start"
            else:
                return "Unexpected character after a closing quote."
        elif char == '"':
            if state != "start":
                return "Quote found inside an unquoted field."
            state = "quoted"
        elif char in ",\r\n":
            state = "start"
        else:
            state = "unquoted"
    return "Unclosed quoted field." if state == "quoted" else None


def inspect_bytes(data: bytes, *, mode: str = "create", header_style: str = "current",
                  variants: bool = False, max_rows: int = MAX_ROWS) -> tuple[dict, list | None]:
    """Return a report and parsed rows. No transformations or value coercion."""
    report = {
        "tool": "repvblicvs-catalog-csv", "version": 1,
        "scope": "Local structure and selected catalog checks only; not a store import guarantee.",
        "profile": {"mode": mode, "header_style": header_style, "variants": variants},
        "reference": SOURCE, "reference_checked": "2026-10-01",
        "input_sha256": hashlib.sha256(data).hexdigest(), "input_bytes": len(data),
        "row_count": 0, "structurally_safe": False, "formula_like_cells": 0,
        "findings": [],
        "unchecked": ["Store state and overwrite behavior", "All Shopify column dependencies",
                      "Inventory, media URLs, taxonomy, tax, and variant identities"],
    }

    def finding(code, message, *, severity="error", kind="structure", **location):
        report["findings"].append({"code": code, "severity": severity, "kind": kind,
                                   "message": message, **location})

    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        finding("invalid_utf8", "Input must be valid UTF-8; encoding is not guessed.")
        return report, None
    if "\x00" in text:
        finding("nul_byte", "NUL characters are refused.")
        return report, None
    issue = _quote_error(text)
    if issue:
        finding("malformed_quoting", issue)
        return report, None
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows, lines = [], []
    previous_limit = csv.field_size_limit(MAX_BYTES)
    try:
        for row in reader:
            if len(rows) > max_rows:
                finding("row_limit", f"Input exceeds the {max_rows} data-row limit.")
                return report, None
            rows.append(row)
            lines.append(reader.line_num)
    except csv.Error:
        finding("malformed_csv", "CSV parser rejected the input.", line=reader.line_num)
        return report, None
    finally:
        csv.field_size_limit(previous_limit)
    if not rows:
        finding("empty_file", "A header row is required.")
        return report, None
    headers = rows[0]
    if not headers or any(not header.strip() for header in headers):
        finding("empty_header", "Every column must have a nonempty header.", row=1)
    if len(set(headers)) != len(headers):
        finding("duplicate_header", "Duplicate headers make column selection ambiguous.", row=1)
    for index, row in enumerate(rows[1:], 2):
        if len(row) != len(headers):
            finding("row_width", f"Expected {len(headers)} columns; found {len(row)}.",
                    row=index, line=lines[index - 1])
    report["row_count"] = len(rows) - 1
    if len(rows) == 1:
        finding("no_data", "No data rows found.", severity="warning", kind="catalog")
    if any(item["kind"] == "structure" and item["severity"] == "error"
           for item in report["findings"]):
        return report, None
    report["structurally_safe"] = True
    names = HEADERS[header_style]
    required = {"Title"}
    if mode == "update" or variants:
        required.add(names["handle"])
    if mode == "update" and any(names[key] in headers for key in ("sku", "weight")):
        required.update((names["option_name"], names["option_value"]))
    for header in sorted(required - set(headers)):
        finding("missing_required_header", f"Selected profile requires the {header!r} column.",
                kind="catalog", row=1)
    other = HEADERS["legacy" if header_style == "current" else "current"]
    if any(value in headers for value in other.values()):
        finding("other_header_style", "Headers from the other profile were found. Select the matching "
                "--header-style; headers are never renamed.", severity="warning", kind="catalog", row=1)
    price_headers = {names["price"], names["compare"], "Cost per item"}
    price_headers.update(header for header in headers if
                         header.startswith("Price / ") or header.startswith("Compare-at price / "))
    price_columns = [(headers.index(header), header) for header in sorted(price_headers & set(headers))]
    seen_skus = {}
    for number, row in enumerate(rows, 1):
        for column, value in enumerate(row):
            if value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
                report["formula_like_cells"] += 1
                finding("formula_like", "Cell starts with a spreadsheet formula marker; normalization "
                        "does not neutralize it.", severity="warning", kind="spreadsheet",
                        row=number, column=column + 1)
        if number == 1:
            continue
        for column, header in price_columns:
            value = row[column]
            if value and not PRICE.fullmatch(value):
                finding("invalid_price", "Expected an unsigned decimal amount without currency "
                        "symbols, separators, exponent, or surrounding whitespace; value is preserved.",
                        kind="catalog", row=number, column=column + 1)
        if names["price"] in headers and not row[headers.index(names["price"])]:
            finding("blank_price", "Blank price is preserved; review its default/import meaning.",
                    severity="warning", kind="catalog", row=number)
        if names["sku"] in headers:
            sku = row[headers.index(names["sku"])]
            if sku:
                if sku in seen_skus:
                    finding("duplicate_sku", f"Nonempty SKU also appears in row {seen_skus[sku]}; "
                            "review whether this is intended.", severity="warning", kind="catalog", row=number)
                else:
                    seen_skus[sku] = number
    return report, rows


def normalized_text(rows: list) -> str:
    output = io.StringIO(newline="")
    # Quoting every field also preserves embedded bare CR with LF record separators.
    csv.writer(output, lineterminator="\n", quoting=csv.QUOTE_ALL).writerows(rows)
    return output.getvalue()


def _write_new(path: Path, text: str, created: list) -> None:
    """Exclusive creation protects the input and any existing output."""
    with path.open("x", encoding="utf-8", newline="") as handle:
        identity = os.fstat(handle.fileno())
        created.append((path, identity.st_dev, identity.st_ino))
        handle.write(text)


def _cleanup_created(created: list) -> bool:
    """Remove only outputs whose current identity still matches our creation."""
    complete = True
    for path, device, inode in reversed(created):
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (device, inode):
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            complete = False
    return complete


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--report", type=Path, help="New JSON report file; defaults to stdout")
    parser.add_argument("--normalize", type=Path, help="New UTF-8 CSV file with normalized quoting/LF records")
    parser.add_argument("--mode", choices=("create", "update"), default="create")
    parser.add_argument("--header-style", choices=tuple(HEADERS), default="current")
    parser.add_argument("--variants", action="store_true", help="Require a handle for a new variants catalog")
    parser.add_argument("--allow-formula-like", action="store_true", help="Acknowledge formula risk and "
                        "allow unchanged formula-like cells in the export")
    args = parser.parse_args(argv)
    destinations = [path for path in (args.report, args.normalize) if path is not None]
    if len({path.resolve() for path in [args.input, *destinations]}) != 1 + len(destinations):
        parser.error("input, report, and normalized output must be distinct paths")
    if any(path.exists() or path.is_symlink() for path in destinations):
        parser.error("output files must not already exist; existing files are never overwritten")
    created = []
    try:
        if any(not path.parent.is_dir() for path in destinations):
            raise FileNotFoundError("output parent directory does not exist")
        with args.input.open("rb") as handle:
            data = handle.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            report = {"tool": "repvblicvs-catalog-csv", "structurally_safe": False,
                      "findings": [{"code": "size_limit", "severity": "error", "kind": "structure",
                                    "message": "Input exceeds the 10 MiB limit."}]}
            rows = None
        else:
            report, rows = inspect_bytes(data, mode=args.mode, header_style=args.header_style,
                                         variants=args.variants)
        blocked = []
        if rows is None:
            blocked.append("structural_errors")
        if report.get("formula_like_cells") and not args.allow_formula_like:
            blocked.append("formula_like_requires_acknowledgment")
        report["normalization"] = {"requested": args.normalize is not None, "written": False,
                                   "blocked_reasons": blocked if args.normalize else [],
                                   "formula_risk_acknowledged": args.allow_formula_like,
                                   "changes": "UTF-8 without BOM, quoting, LF record separators only; "
                                              "parsed cell values and header order preserved."}
        if args.normalize and not blocked:
            _write_new(args.normalize, normalized_text(rows), created)
            report["normalization"]["written"] = True
        serialized = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
        if args.report:
            _write_new(args.report, serialized, created)
        else:
            sys.stdout.write(serialized)
        return int(any(item["severity"] == "error" for item in report["findings"])
                   or bool(args.normalize and blocked))
    except OSError as error:
        cleanup_complete = _cleanup_created(created)
        # Avoid disclosing absolute local paths in diagnostics.
        sys.stderr.write(f"File operation failed ({type(error).__name__}); check paths and permissions.\n")
        if not cleanup_complete:
            sys.stderr.write("Cleanup could not remove every output created by this command; inspect output paths.\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
