"""Deterministic delivery workflows with reproducible, inspectable artifacts.

These workflows do not contact a model, publish customer data, or make scientific
claims beyond the checks recorded in their output. Customer artifacts belong in
private runtime storage, never in the source repository.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


class WorkflowError(ValueError):
    """A stable failure code suitable for task routing without a traceback."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_ROWS = 100_000


def _write(directory: Path, name: str, content: str) -> str:
    target = directory / name
    target.write_text(content, encoding="utf-8")
    target.chmod(0o600)
    return name


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _finish(directory: Path, kind: str, summary: dict, artifacts: list[str], evidence: list[dict]) -> dict:
    manifest = {
        "workflow": kind,
        "files": [
            {"name": name, "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest()}
            for name in artifacts
        ],
        "evidence": evidence,
    }
    artifacts.append(_write(directory, "manifest.json", _json(manifest)))
    return {"kind": kind, "status": "completed", "summary": summary, "artifacts": artifacts, "evidence": evidence}


def _number(value: str) -> Decimal | None:
    if len(value) > 256:
        return None
    try:
        number = Decimal(value)
        return number if number.is_finite() and abs(number.as_tuple().exponent) <= 308 else None
    except (InvalidOperation, ValueError):
        return None


def _csv_analysis(headers: list[str], rows: list[list[str]]) -> dict:
    result = {"rows": len(rows), "columns": {}}
    for index, header in enumerate(headers):
        values = [row[index] for row in rows]
        numbers = [number for value in values if value and (number := _number(value)) is not None]
        column = {"missing": values.count(""), "nonempty": sum(bool(value) for value in values), "numeric": len(numbers)}
        if numbers:
            precision = max(50, max(number.adjusted() for number in numbers) - min(number.as_tuple().exponent for number in numbers) + len(str(len(numbers))) + 3)
            with localcontext() as context:
                context.prec = precision
                total = sum(numbers, Decimal(0))
                column.update({"sum": str(total), "min": str(min(numbers)), "max": str(max(numbers)), "mean": str(total / len(numbers)), "mean_decimal_precision": precision})
        result["columns"][header] = column
    return result


CSV_ANALYSIS_SCRIPT = '''#!/usr/bin/env python3
"""Reproduce analysis.json using only Python's standard library."""
import csv, json
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
root = Path(__file__).resolve().parent
with (root / "cleaned.csv").open(encoding="utf-8", newline="") as stream:
    reader = csv.reader(stream)
    headers, rows = next(reader), list(reader)
result = {"rows": len(rows), "columns": {}}
for index, header in enumerate(headers):
    values = [row[index] for row in rows]
    numbers = []
    for value in values:
        if value:
            try:
                number = Decimal(value)
                if len(value) <= 256 and number.is_finite() and abs(number.as_tuple().exponent) <= 308: numbers.append(number)
            except InvalidOperation: pass
    column = {"missing": values.count(""), "nonempty": sum(bool(v) for v in values), "numeric": len(numbers)}
    if numbers:
        precision = max(50, max(n.adjusted() for n in numbers)-min(n.as_tuple().exponent for n in numbers)+len(str(len(numbers)))+3)
        with localcontext() as context:
            context.prec = precision
            total = sum(numbers, Decimal(0))
            column.update({"sum": str(total), "min": str(min(numbers)), "max": str(max(numbers)), "mean": str(total/len(numbers)), "mean_decimal_precision": precision})
    result["columns"][header] = column
print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
'''


def _csv_cleanup(payload: dict, output: Path) -> dict:
    if ("csv_text" in payload) == ("input_path" in payload):
        raise WorkflowError("invalid_input", "Provide exactly one of csv_text or input_path.")
    if "input_path" in payload:
        source = Path(payload["input_path"])
        if not source.is_file() or source.stat().st_size > MAX_INPUT_BYTES:
            raise WorkflowError("invalid_input", "CSV input must be a file no larger than 10 MiB.")
        try:
            text = source.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise WorkflowError("invalid_encoding", "CSV input must be readable UTF-8.") from exc
    else:
        text = payload["csv_text"]
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise WorkflowError("invalid_input", "CSV text must be a string no larger than 10 MiB.")
    delimiter = payload.get("delimiter", ",")
    if delimiter not in (",", ";", "\t", "|"):
        raise WorkflowError("invalid_delimiter", "Supported delimiters are comma, semicolon, tab, and pipe.")
    if "remove_duplicates" in payload and not isinstance(payload["remove_duplicates"], bool):
        raise WorkflowError("invalid_input", "remove_duplicates must be a boolean.")
    try:
        reader = csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=delimiter, strict=True)
        original_headers = next(reader)
        raw_rows = list(reader)
    except (StopIteration, csv.Error) as exc:
        raise WorkflowError("malformed_csv", "CSV must contain a valid header and well-formed quoted fields.") from exc
    if not original_headers or len(original_headers) > 1000 or len(raw_rows) > MAX_ROWS:
        raise WorkflowError("input_limit", "CSV must have 1–1000 columns and at most 100,000 data rows.")
    headers, used = [], set()
    for index, value in enumerate(original_headers, start=1):
        base = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_") or f"column_{index}"
        normalized, suffix = base, 2
        while normalized in used:
            normalized, suffix = f"{base}_{suffix}", suffix + 1
        used.add(normalized)
        headers.append(normalized)
    rows, seen, empty, duplicates, formula_fields = [], set(), 0, 0, 0
    for index, raw in enumerate(raw_rows, start=2):
        if not raw or not any(value.strip() for value in raw):
            empty += 1
            continue
        if len(raw) != len(headers):
            raise WorkflowError("ragged_csv", f"Row {index} does not match the header width.")
        row = [value.strip() for value in raw]
        for field, value in enumerate(row):
            if value and value[0] in "=+-@" and _number(value) is None:
                row[field] = "'" + value
                formula_fields += 1
        key = tuple(row)
        if payload.get("remove_duplicates", False) and key in seen:
            duplicates += 1
            continue
        seen.add(key)
        rows.append(row)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    cleaned = stream.getvalue()
    if list(csv.reader(io.StringIO(cleaned))) != [headers, *rows]:
        raise WorkflowError("validation_failed", "Normalized CSV failed its round-trip check.")
    analysis = _csv_analysis(headers, rows)
    acceptance = payload.get("acceptance", {})
    if not isinstance(acceptance, dict): raise WorkflowError("invalid_input", "acceptance must be an object.")
    if "expected_output_rows" in acceptance and acceptance["expected_output_rows"] != len(rows):
        raise WorkflowError("acceptance_failed", "Output row count does not meet the requested acceptance criterion.")
    required = acceptance.get("required_columns", [])
    numeric = acceptance.get("numeric_columns", [])
    if not isinstance(required, list) or not isinstance(numeric, list) or any(not isinstance(column, str) for column in required + numeric):
        raise WorkflowError("invalid_input", "Acceptance column lists must contain normalized names.")
    if any(column not in headers for column in required + numeric):
        raise WorkflowError("acceptance_failed", "A required normalized column is missing.")
    if any(analysis["columns"][column]["numeric"] != analysis["columns"][column]["nonempty"] for column in numeric):
        raise WorkflowError("acceptance_failed", "A required numeric column contains nonnumeric values.")
    summary = {"input_rows": len(raw_rows), "output_rows": len(rows), "columns": len(headers), "empty_rows_removed": empty, "duplicates_removed": duplicates, "formula_fields_escaped": formula_fields, "duplicate_policy": "remove" if payload.get("remove_duplicates", False) else "preserve"}
    report = "# CSV delivery report\n\n" + "\n".join(f"- {name.replace('_', ' ').capitalize()}: {value}" for name, value in summary.items())
    report += "\n\nHeaders were normalized uniquely; their source-to-output mapping is included. Surrounding field whitespace was trimmed. Duplicate records are preserved unless removal was explicitly requested. Spreadsheet formulas in textual fields were escaped with a leading apostrophe. Numeric totals use sufficient decimal precision for the bounded values; means disclose their precision. Numeric classification is limited to 256-character values and decimal exponents within ±308. No missing value was imputed.\n\nRun `python3 analyze.py` in this delivery directory to reproduce `analysis.json`. The source file is never modified.\n"
    evidence = [{"check": "csv_round_trip", "result": "passed"}, {"check": "unique_headers", "result": "passed"}, {"check": "row_width", "result": "passed"}, {"check": "missing_values", "result": "reported_without_imputation"}, {"check": "requested_acceptance", "result": "passed" if acceptance else "not_supplied"}]
    artifacts = [_write(output, "cleaned.csv", cleaned), _write(output, "analysis.json", _json(analysis)), _write(output, "header-map.json", _json([{"original": old, "normalized": new} for old, new in zip(original_headers, headers)])), _write(output, "analyze.py", CSV_ANALYSIS_SCRIPT), _write(output, "report.md", report)]
    return _finish(output, "csv_cleanup", summary, artifacts, evidence)


def _render_markdown(text: str) -> str:
    blocks, paragraph, in_list, in_code = [], [], False, False
    def flush() -> None:
        if paragraph:
            blocks.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()
    for line in text.splitlines():
        if line.startswith("```"):
            flush()
            if in_list:
                blocks.append("</ul>"); in_list = False
            blocks.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
        elif in_code:
            blocks.append(html.escape(line) + "\n")
        else:
            heading = re.match(r"^(#{1,6})\s+(.+)$", line)
            bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
            if not bullet and in_list:
                blocks.append("</ul>"); in_list = False
            if heading:
                flush(); depth = len(heading[1]); blocks.append(f"<h{depth}>{html.escape(heading[2])}</h{depth}>")
            elif bullet:
                flush()
                if not in_list:
                    blocks.append("<ul>"); in_list = True
                blocks.append("<li>" + html.escape(bullet[1]) + "</li>")
            elif line.strip():
                paragraph.append(html.escape(line))
            else:
                flush()
    flush()
    if in_list: blocks.append("</ul>")
    if in_code: blocks.append("</code></pre>")
    return "\n".join(blocks)


def _character_svg(spec: dict) -> str:
    species = spec.get("species", "fox")
    if species not in {"fox", "cat", "wolf", "rabbit"} or spec.get("pose", "portrait") != "portrait" or spec.get("explicit", False):
        raise WorkflowError("unsupported_illustration", "Supported illustrations are non-explicit geometric fox, cat, wolf, or rabbit portraits.")
    color = {"fox": "#c65d27", "cat": "#ad8064", "wolf": "#778798", "rabbit": "#a99fa8"}[species]
    ears = '<ellipse cx="180" cy="115" rx="28" ry="91"/><ellipse cx="332" cy="115" rx="28" ry="91"/>' if species == "rabbit" else '<path d="M120 225 124 65 230 180Z"/><path d="M282 180 388 65 392 225Z"/>'
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-labelledby="title desc">
<title id="title">Geometric {species} portrait</title><desc id="desc">An original non-explicit vector character illustration made from geometric shapes.</desc>
<rect width="512" height="512" rx="36" fill="#f3efe7"/><circle cx="256" cy="300" r="175" fill="#e7ddc9"/>
<g fill="{color}">{ears}<path d="M111 213 Q256 133 401 213 L373 358 Q256 451 139 358Z"/></g>
<path d="M139 288 235 322 256 390 173 358Z M373 288 277 322 256 390 339 358Z" fill="#fff7e9"/>
<ellipse cx="198" cy="270" rx="16" ry="20" fill="#26313c"/><ellipse cx="314" cy="270" rx="16" ry="20" fill="#26313c"/>
<circle cx="202" cy="264" r="5" fill="white"/><circle cx="318" cy="264" r="5" fill="white"/>
<path d="M238 327 Q256 313 274 327 L256 343Z" fill="#26313c"/><path d="M256 343 V359 M232 359 Q256 383 280 359" stroke="#26313c" stroke-width="7" fill="none" stroke-linecap="round"/>
</svg>\n'''


class _DocumentValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.text: list[str] = []
        self.counts: dict[str, int] = {}
        self.valid = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.counts[tag] = self.counts.get(tag, 0) + 1
        if tag in {"script", "iframe", "object", "embed", "form"}: self.valid = False
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}: self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag: self.valid = False
        else: self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _document_package(payload: dict, output: Path) -> dict:
    title, content = payload.get("title", "Delivery brief"), payload.get("content", payload.get("brief"))
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or not isinstance(content, str) or not content.strip() or len(content) > 100_000:
        raise WorkflowError("invalid_input", "A document needs a nonempty title and content of at most 100,000 characters.")
    body, artifacts = _render_markdown(content), []
    sources = payload.get("sources", [])
    if not isinstance(sources, list) or len(sources) > 50: raise WorkflowError("invalid_input", "sources must be a list of at most 50 title/URL objects.")
    if sources:
        from .opportunities import valid_source_url
        items = []
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get("title"), str) or not source["title"].strip() or len(source["title"]) > 300 or not valid_source_url(source.get("url")):
                raise WorkflowError("invalid_source", "Document sources require a bounded title and a valid public HTTP(S) URL.")
            items.append(f'<li><a href="{html.escape(source["url"], quote=True)}" rel="noopener noreferrer">{html.escape(source["title"])}</a></li>')
        body += '<section><h2>Sources supplied for this document</h2><ol>' + "".join(items) + '</ol><p>Source URL structure was checked. Content accuracy and source support require substantive review.</p></section>'
    illustration = payload.get("illustration")
    if illustration is not None:
        if not isinstance(illustration, dict):
            raise WorkflowError("invalid_input", "illustration must explicitly specify a supported portrait.")
        svg = _character_svg(illustration)
        artifacts.append(_write(output, "character.svg", svg))
        body += '<figure><img src="character.svg" alt="Original geometric character portrait"><figcaption>Original geometric vector illustration; no generative image model was used.</figcaption></figure>'
    safe_title = html.escape(title.strip())
    document = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{safe_title}</title>
<style>:root{{font-family:system-ui,sans-serif;color:#1f2933;background:#eef1f5}}body{{margin:0;padding:3rem 1rem}}main{{max-width:800px;margin:auto;padding:3rem;background:white;border-top:7px solid #256968;box-shadow:0 12px 50px #1f293315;border-radius:8px}}header p{{text-transform:uppercase;letter-spacing:.14em;font-size:.75rem;color:#52716f}}h1{{font-size:2.3rem;line-height:1.15}}h2,h3{{color:#256968;margin-top:2rem}}p,li{{line-height:1.7}}pre{{overflow:auto;padding:1rem;background:#f4f6f8;border-radius:5px}}figure{{margin:2rem 0}}img{{width:100%;max-width:400px}}figcaption,footer{{font-size:.8rem;color:#52606d}}footer{{border-top:1px solid #d9e2ec;margin-top:3rem;padding-top:1rem}}@media print{{body{{background:white;padding:0}}main{{box-shadow:none;padding:1rem}}}}</style></head>
<body><main><header><p>Repvblicvs · delivery</p><h1>{safe_title}</h1></header><article>{body}</article><footer>Self-contained document package. Source brief and file hashes are included.</footer></main></body></html>\n'''
    validator = _DocumentValidator()
    validator.feed(document)
    if not validator.valid or validator.stack or validator.counts.get("main") != 1 or validator.counts.get("article") != 1 or 'src="http' in document.lower():
        raise WorkflowError("validation_failed", "Document validation rejected an executable or remote dependency.")
    rendered_text = " ".join(" ".join(validator.text).split())
    for line in content.splitlines():
        if line.startswith("```"): continue
        expected = re.sub(r"^(?:#{1,6}\s+|\s*[-*]\s+)", "", line).strip()
        if expected and " ".join(expected.split()) not in rendered_text:
            raise WorkflowError("validation_failed", "Document content was not preserved in the rendered package.")
    required_terms = payload.get("required_terms", [])
    if not isinstance(required_terms, list) or any(not isinstance(term, str) or not term for term in required_terms):
        raise WorkflowError("invalid_input", "required_terms must be a list of nonempty strings.")
    if any(term.casefold() not in content.casefold() for term in required_terms):
        raise WorkflowError("acceptance_failed", "Document is missing a required acceptance term.")
    artifacts += [_write(output, "brief.md", f"# {title.strip()}\n\n{content.strip()}\n"), _write(output, "deliverable.html", document)]
    summary = {"title": title.strip(), "content_characters": len(content), "source_count": len(sources), "illustration": illustration.get("species", "fox") if illustration else None, "format": "markdown_and_standalone_html"}
    evidence = [{"check": "content_preserved", "result": "passed"}, {"check": "html_structure", "result": "passed"}, {"check": "html_escaped", "result": "passed"}, {"check": "remote_dependencies", "result": "none"}, {"check": "source_url_structure", "result": "passed" if sources else "not_supplied"}, {"check": "source_content_verification", "result": "requires_substantive_review" if sources else "not_claimed"}, {"check": "required_terms", "result": "passed" if required_terms else "not_supplied"}]
    if sources: artifacts.append(_write(output, "sources.json", _json(sources)))
    if illustration: evidence.append({"check": "illustration_origin", "result": "original_geometric_svg_no_model"})
    return _finish(output, "document_package", summary, artifacts, evidence)


def _solve(rows: list[list[Fraction]], rhs: list[Fraction], width: int) -> list[Fraction] | None:
    matrix = [row[:] + [value] for row, value in zip(rows, rhs)]
    pivot_row, pivots = 0, []
    for column in range(width):
        pivot = next((r for r in range(pivot_row, len(matrix)) if matrix[r][column]), None)
        if pivot is None: continue
        matrix[pivot_row], matrix[pivot] = matrix[pivot], matrix[pivot_row]
        divisor = matrix[pivot_row][column]
        matrix[pivot_row] = [value / divisor for value in matrix[pivot_row]]
        for r in range(len(matrix)):
            if r != pivot_row and matrix[r][column]:
                scale = matrix[r][column]
                matrix[r] = [a - scale * b for a, b in zip(matrix[r], matrix[pivot_row])]
        pivots.append(column); pivot_row += 1
    if any(not any(row[:width]) and row[-1] for row in matrix) or len(pivots) != width: return None
    result = [Fraction(0)] * width
    for r, column in enumerate(pivots): result[column] = matrix[r][-1]
    return result


def _candidate_predictions(model: dict, training: list[Fraction], count: int) -> list[Fraction]:
    coefficients = [Fraction(value) for value in model["coefficients"]]
    if model["family"] == "polynomial":
        return [sum((coefficient * index ** degree for degree, coefficient in enumerate(coefficients)), Fraction(0)) for index in range(len(training), len(training) + count)]
    values = training[:]
    for _ in range(count): values.append(sum((coefficient * values[-i - 1] for i, coefficient in enumerate(coefficients)), Fraction(0)))
    return values[len(training):]


SEQUENCE_SCRIPT = '''#!/usr/bin/env python3
"""Replay accepted exact candidates without importing the engine."""
import json
from fractions import Fraction
from pathlib import Path
study = json.loads((Path(__file__).resolve().parent / "study.json").read_text())
training = [Fraction(v) for v in study["training"]]
heldout = [Fraction(v) for v in study["heldout"]]
results = []
for model in study["accepted_candidates"]:
    coefficients = [Fraction(v) for v in model["coefficients"]]
    if model["family"] == "polynomial":
        predicted = [sum((c * n ** i for i,c in enumerate(coefficients)), Fraction(0)) for n in range(len(training), len(training)+len(heldout)+1)]
    else:
        values = training[:]
        for _ in range(len(heldout)+1): values.append(sum((c * values[-i-1] for i,c in enumerate(coefficients)), Fraction(0)))
        predicted = values[len(training):]
    results.append({"family": model["family"], "heldout_pass": predicted[:-1] == heldout, "next_prediction": str(predicted[-1])})
print(json.dumps({"status": study["status"], "results": results}, indent=2, sort_keys=True))
'''


def _research_sequence(payload: dict, output: Path) -> dict:
    raw = payload.get("values")
    if not isinstance(raw, list) or not 2 <= len(raw) <= 32 or any(isinstance(value, (bool, float)) or not isinstance(value, (str, int)) for value in raw):
        raise WorkflowError("invalid_input", "Provide 2–32 integer or exact rational string values; floating-point inputs are not exact evidence.")
    try:
        values = [Fraction(value) for value in raw]
    except (ValueError, ZeroDivisionError) as exc:
        raise WorkflowError("invalid_input", "Sequence values must be valid exact rationals.") from exc
    if any(value.numerator.bit_length() > 128 or value.denominator.bit_length() > 128 for value in values):
        raise WorkflowError("input_limit", "Sequence numerators and denominators are limited to 128 bits.")
    holdout, max_degree, max_order = payload.get("holdout", 2), payload.get("max_degree", 4), payload.get("max_order", 3)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (holdout, max_degree, max_order)) or not 1 <= holdout < len(values) or not 0 <= max_degree <= 4 or not 0 <= max_order <= 3:
        raise WorkflowError("invalid_bounds", "Use a valid holdout, polynomial degree 0–4, and recurrence order 0–3.")
    training, heldout = values[:-holdout], values[-holdout:]
    candidates = []
    for degree in range(min(max_degree, len(training) - 2) + 1):
        coefficients = _solve([[Fraction(n ** exponent) for exponent in range(degree + 1)] for n in range(len(training))], training, degree + 1)
        if coefficients is not None:
            candidates.append({"family": "polynomial", "degree": degree, "coefficients": [str(c) for c in coefficients]})
    for order in range(1, min(max_order, len(training) // 2) + 1):
        coefficients = _solve([[training[n - i - 1] for i in range(order)] for n in range(order, len(training))], training[order:], order)
        if coefficients is not None:
            candidates.append({"family": "linear_recurrence", "order": order, "coefficients": [str(c) for c in coefficients]})
    accepted, rejected = [], []
    for model in candidates:
        predictions = _candidate_predictions(model, training, holdout + 1)
        model["heldout_predictions"] = [str(v) for v in predictions[:-1]]
        model["next_prediction"] = str(predictions[-1])
        (accepted if predictions[:-1] == heldout else rejected).append(model)
    predictions = {model["next_prediction"] for model in accepted}
    if len(training) < 4 or holdout < 2:
        status = "ambiguous"
    elif not accepted:
        status = "unsupported"
    elif len(predictions) > 1:
        status = "ambiguous"
    else:
        status = "supported_within_search_bounds"
    study = {"status": status, "training": [str(v) for v in training], "heldout": [str(v) for v in heldout], "search_bounds": {"polynomial_degree": max_degree, "homogeneous_linear_recurrence_order": max_order}, "accepted_candidates": accepted, "rejected_candidates": rejected, "next_observation": {"index": len(values), "candidate_predictions": sorted(predictions)}, "limitation": "Finite agreement does not prove a generating rule. These are exact checks within explicitly bounded candidate families, not a Lean/kernel proof or a claim of novelty."}
    report = f"# Exact sequence experiment\n\nStatus: **{status}**.\n\n{len(training)} training values and {len(heldout)} untouched held-out values were checked using rational arithmetic. Search bounds: polynomial degree ≤ {max_degree}; homogeneous linear recurrence order ≤ {max_order}.\n\nAccepted candidates: {len(accepted)}. Rejected by held-out evidence: {len(rejected)}.\n\nNext useful observation: value at zero-based index {len(values)}. Candidate predictions: {', '.join(sorted(predictions)) or 'none supported'}.\n\n{study['limitation']}\n\nRun `python3 experiment.py` to replay the accepted candidates. No source files or external service are required.\n"
    evidence = [{"check": "arithmetic", "result": "exact_rational"}, {"check": "heldout_validation", "result": status}, {"check": "generating_rule_proof", "result": "not_claimed"}]
    artifacts = [_write(output, "study.json", _json(study)), _write(output, "experiment.py", SEQUENCE_SCRIPT), _write(output, "report.md", report)]
    return _finish(output, "research_sequence", {"status": status, "accepted_candidates": len(accepted), "next_predictions": sorted(predictions)}, artifacts, evidence)


def run_workflow(kind: str, payload: dict, output_dir: Path) -> dict:
    """Produce a private delivery package; returned artifact paths are relative."""
    if not isinstance(payload, dict): raise WorkflowError("invalid_input", "Workflow payload must be an object.")
    supported = {"csv_cleanup": {"csv_text", "input_path", "delimiter", "remove_duplicates", "acceptance"},
                 "document_package": {"title", "content", "brief", "sources", "illustration", "required_terms"},
                 "research_sequence": {"values", "holdout", "max_degree", "max_order"}}
    if kind in supported and set(payload) - supported[kind]:
        raise WorkflowError("unsupported_field", "An unrecognized requested field must be resolved before delivery.")
    if kind == "csv_cleanup" and isinstance(payload.get("acceptance"), dict) and set(payload["acceptance"]) - {"expected_output_rows", "required_columns", "numeric_columns"}:
        raise WorkflowError("unsupported_field", "A requested acceptance check is unavailable.")
    if kind == "document_package" and isinstance(payload.get("illustration"), dict) and set(payload["illustration"]) - {"species", "pose", "explicit"}:
        raise WorkflowError("unsupported_field", "A requested illustration attribute is unavailable.")
    functions = {"csv_cleanup": _csv_cleanup, "document_package": _document_package, "research_sequence": _research_sequence}
    if kind in {"json_preflight", "catalog_inspect"}:
        from .inspection import run_inspection
        functions[kind] = lambda value, directory: run_inspection(kind, value, directory)
    if kind not in functions: raise WorkflowError("unsupported_workflow", "Workflow kind is not available.")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(output.iterdir()): raise WorkflowError("output_not_empty", "Use a new empty delivery directory to prevent overwriting prior work.")
    try:
        return functions[kind](payload, output)
    except WorkflowError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise WorkflowError("workflow_failed", "Workflow input or output could not be processed.") from exc
