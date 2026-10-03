"""Package reviewed local software/data diagnostics without altering input."""
from pathlib import Path
import hashlib
import json
from ._vendor import catalog_csv, json_preflight


def run_inspection(kind: str, payload: dict, output_dir: Path) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(output_dir.iterdir()):
        raise ValueError("Inspection output must be a new empty directory")
    text = payload.get("text", payload.get("csv_text"))
    if not isinstance(text, str):
        raise ValueError("A text source is required")
    source = text.encode("utf-8")
    if len(source) > 1_000_000:
        raise ValueError("Inspection input exceeds the 1 MB delivery limit")
    if kind == "json_preflight":
        report = json_preflight.inspect_bytes(source)
        suffix = "json"
    elif kind == "catalog_inspect":
        mode = payload.get("mode", "create")
        header_style = payload.get("header_style", "current")
        if mode not in {"create", "update"} or header_style not in {"current", "legacy"}:
            raise ValueError("Unsupported catalog mode or header profile")
        report, _ = catalog_csv.inspect_bytes(source, mode=mode, header_style=header_style)
        suffix = "csv"
    else:
        raise ValueError("Unsupported inspection workflow")
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
