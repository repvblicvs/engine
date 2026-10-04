"""Package reviewed local software/data diagnostics without altering input."""
from pathlib import Path
import hashlib
import json
from ._vendor import catalog_csv, json_preflight
from .workflows import WorkflowError


INSPECTION_FIELDS = {
    "json_preflight": {"text", "csv_text"},
    "catalog_inspect": {"text", "csv_text", "mode", "header_style"},
}


def validate_inspection(kind: str, payload: dict) -> bytes:
    """Reject unimplemented customer requests before creating any artifacts."""
    if kind not in INSPECTION_FIELDS:
        raise WorkflowError("unsupported_workflow", "Inspection workflow kind is not available.")
    if not isinstance(payload, dict):
        raise WorkflowError("invalid_input", "Inspection payload must be an object.")
    if set(payload) - INSPECTION_FIELDS[kind]:
        raise WorkflowError("unsupported_field", "An unrecognized requested inspection field must be resolved before delivery.")
    if ("text" in payload) == ("csv_text" in payload):
        raise WorkflowError("invalid_input", "Provide exactly one of text or csv_text for inspection.")
    text = payload.get("text", payload.get("csv_text"))
    if not isinstance(text, str):
        raise WorkflowError("invalid_input", "A text source is required.")
    try:
        source = text.encode("utf-8")
    except UnicodeError as exc:
        raise WorkflowError("invalid_input", "Inspection input must be valid UTF-8 text.") from exc
    if len(source) > 1_000_000:
        raise WorkflowError("input_limit", "Inspection input exceeds the 1 MB delivery limit.")
    if kind == "catalog_inspect" and (payload.get("mode", "create") not in ("create", "update") or payload.get("header_style", "current") not in ("current", "legacy")):
        raise WorkflowError("invalid_input", "Unsupported catalog mode or header profile.")
    return source


def run_inspection(kind: str, payload: dict, output_dir: Path) -> dict:
    source = validate_inspection(kind, payload)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(output_dir.iterdir()):
        raise WorkflowError("output_not_empty", "Inspection output must be a new empty directory.")
    if kind == "json_preflight":
        report = json_preflight.inspect_bytes(source)
        suffix = "json"
    elif kind == "catalog_inspect":
        mode = payload.get("mode", "create")
        header_style = payload.get("header_style", "current")
        report, _ = catalog_csv.inspect_bytes(source, mode=mode, header_style=header_style)
        suffix = "csv"
    source_path = output_dir / ("original." + suffix)
    source_path.write_bytes(source)
    (output_dir / "inspection.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    (output_dir / "README.md").write_text(
        "# Inspection delivery\n\nThe original input is preserved byte for byte. "
        "Read inspection.json for bounded structural findings. The completed package "
        "does not mean the input passed validation or a destination accepted an import.\n"
        "\nTool provenance and the MIT license are included in the installed package.\n"
    )
    original_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    expected_hash = hashlib.sha256(source).hexdigest()
    if original_hash != expected_hash:
        raise RuntimeError("Original source preservation failed")
    return {"kind": kind, "status": "completed", "summary": report,
            "artifacts": [source_path.name, "inspection.json", "README.md"],
            "evidence": [{"check": "original input hash", "result": "passed", "sha256": original_hash},
                         {"check": "scope", "result": "local structural diagnostics only"}]}
