import csv
import hashlib
import io
import json
import subprocess
import sys

import pytest

from repvblicvs_engine.workflows import WorkflowError, run_workflow


def test_csv_delivery_replays_exactly(tmp_path):
    source = ' Name , Name , Amount , Note\n Alice , blue , 10.25 ,"hello, world"\n Bob , green , -2.25 ,=SUM(A1:A9)\n Bob , green , -2.25 ,=SUM(A1:A9)\n,,,\n'
    result = run_workflow("csv_cleanup", {"csv_text": source}, tmp_path / "csv")
    assert result["status"] == "completed"
    assert result["summary"]["output_rows"] == 3
    assert result["summary"]["duplicates_removed"] == 0
    rows = list(csv.reader(io.StringIO((tmp_path / "csv" / "cleaned.csv").read_text())))
    assert rows[0] == ["name", "name_2", "amount", "note"]
    assert rows[1] == ["Alice", "blue", "10.25", "hello, world"]
    assert rows[2][-1].startswith("'=")
    assert rows[2][2] == "-2.25"
    original_analysis = json.loads((tmp_path / "csv" / "analysis.json").read_text())
    replay = subprocess.run([sys.executable, str(tmp_path / "csv" / "analyze.py")], check=True, capture_output=True, text=True)
    assert json.loads(replay.stdout) == original_analysis
    assert original_analysis["columns"]["amount"]["sum"] == "5.75"
    manifest = json.loads((tmp_path / "csv" / "manifest.json").read_text())
    for item in manifest["files"]:
        assert hashlib.sha256((tmp_path / "csv" / item["name"]).read_bytes()).hexdigest() == item["sha256"]


def test_csv_explicit_duplicate_removal_and_missing_values(tmp_path):
    result = run_workflow("csv_cleanup", {"csv_text": "a,b\n1,\n1,\n2,3\n", "remove_duplicates": True}, tmp_path)
    assert result["summary"]["duplicates_removed"] == 1
    analysis = json.loads((tmp_path / "analysis.json").read_text())
    assert analysis["columns"]["b"]["missing"] == 1
    assert analysis["rows"] == 2


def test_csv_exact_large_total_and_customer_acceptance(tmp_path):
    large = "1234567890123456789012345678901234567890.01"
    result = run_workflow("csv_cleanup", {"csv_text": "total\n" + large + "\n0.02\n", "acceptance": {"expected_output_rows": 2, "required_columns": ["total"], "numeric_columns": ["total"]}}, tmp_path)
    analysis = json.loads((tmp_path / "analysis.json").read_text())
    assert analysis["columns"]["total"]["sum"] == "1234567890123456789012345678901234567890.03"
    replay = subprocess.run([sys.executable, str(tmp_path / "analyze.py")], check=True, capture_output=True, text=True)
    assert json.loads(replay.stdout) == analysis
    assert {item["check"]: item["result"] for item in result["evidence"]}["requested_acceptance"] == "passed"


def test_csv_customer_acceptance_failure_is_not_a_delivery(tmp_path):
    with pytest.raises(WorkflowError) as error:
        run_workflow("csv_cleanup", {"csv_text": "total\nnot-a-number\n", "acceptance": {"numeric_columns": ["total"]}}, tmp_path)
    assert error.value.code == "acceptance_failed"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("source,code", [("a,b\n1,2,3\n", "ragged_csv"), ('a,b\n"unterminated,2\n', "malformed_csv"), ("", "malformed_csv")])
def test_csv_rejects_corrupt_rows(tmp_path, source, code):
    with pytest.raises(WorkflowError) as error:
        run_workflow("csv_cleanup", {"csv_text": source}, tmp_path)
    assert error.value.code == code


def test_document_package_escapes_content_and_supports_explicit_portrait(tmp_path):
    result = run_workflow("document_package", {"title": "Client brief", "content": "## Scope\n\n<script>alert('unsafe')</script>\n\n- Deliver a report\n- Check all totals", "illustration": {"species": "fox"}}, tmp_path)
    document = (tmp_path / "deliverable.html").read_text()
    assert "<script>" not in document
    assert "&lt;script&gt;" in document
    assert "<h2>Scope</h2>" in document
    assert "character.svg" in result["artifacts"]
    assert "no generative image model" in document
    assert "Geometric fox portrait" in (tmp_path / "character.svg").read_text()


def test_document_unsupported_creative_request_is_typed(tmp_path):
    with pytest.raises(WorkflowError) as error:
        run_workflow("document_package", {"content": "Draw a portrait", "illustration": {"species": "dragon"}}, tmp_path)
    assert error.value.code == "unsupported_illustration"
    assert not list(tmp_path.iterdir())


def test_document_sources_structure_and_requested_terms(tmp_path):
    result = run_workflow("document_package", {"title": "Sourced report", "content": "## Scope\n\nA reproducible workflow.\n\n```text\n<literal>\n```", "sources": [{"title": "Official reference", "url": "https://example.com/docs"}], "required_terms": ["reproducible"]}, tmp_path)
    evidence = {item["check"]: item["result"] for item in result["evidence"]}
    assert evidence["html_structure"] == "passed"
    assert evidence["source_url_structure"] == "passed"
    assert evidence["source_content_verification"] == "requires_substantive_review"
    assert "sources.json" in result["artifacts"]
    assert "&lt;literal&gt;" in (tmp_path / "deliverable.html").read_text()


def test_document_rejects_malformed_sources(tmp_path):
    with pytest.raises(WorkflowError) as error:
        run_workflow("document_package", {"content": "Scope", "sources": [{"title": "Untrusted", "url": "javascript:alert(1)"}]}, tmp_path)
    assert error.value.code == "invalid_source"


def test_sequence_supported_case_replays_heldout(tmp_path):
    result = run_workflow("research_sequence", {"values": [n * n for n in range(10)]}, tmp_path)
    assert result["summary"]["status"] == "supported_within_search_bounds"
    assert result["summary"]["next_predictions"] == ["100"]
    replay = subprocess.run([sys.executable, str(tmp_path / "experiment.py")], check=True, capture_output=True, text=True)
    checks = json.loads(replay.stdout)
    assert checks["results"] and all(item["heldout_pass"] for item in checks["results"])
    assert all(item["next_prediction"] == "100" for item in checks["results"])
    assert "not a Lean/kernel proof" in (tmp_path / "report.md").read_text()


def test_sequence_ambiguous_and_unsupported_are_honest(tmp_path):
    ambiguous = run_workflow("research_sequence", {"values": [1, 2, 3]}, tmp_path / "ambiguous")
    unsupported = run_workflow("research_sequence", {"values": [1, 7, 2, 11, 0, -5, 19, 8]}, tmp_path / "unsupported")
    assert ambiguous["summary"]["status"] == "ambiguous"
    assert unsupported["summary"]["status"] == "unsupported"
    assert not unsupported["summary"]["next_predictions"]


def test_sequence_floating_point_is_not_exact_evidence(tmp_path):
    with pytest.raises(WorkflowError) as error:
        run_workflow("research_sequence", {"values": [0.1, 0.2, 0.3]}, tmp_path)
    assert error.value.code == "invalid_input"


def test_delivery_never_overwrites_old_work(tmp_path):
    (tmp_path / "old.txt").write_text("preserve")
    with pytest.raises(WorkflowError) as error:
        run_workflow("csv_cleanup", {"csv_text": "a\n1\n"}, tmp_path)
    assert error.value.code == "output_not_empty"
    assert (tmp_path / "old.txt").read_text() == "preserve"
