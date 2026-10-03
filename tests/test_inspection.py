import json
from repvblicvs_engine.inspection import run_inspection


def test_duplicate_keys_are_not_silently_lost(tmp_path):
    source = '{"catalog":{"price":12,"price":13}}'
    result = run_inspection("json_preflight", {"text": source}, tmp_path)
    assert (tmp_path / "original.json").read_text() == source
    report = json.loads((tmp_path / "inspection.json").read_text())
    assert report["duplicate_count"] == 1
    assert report["status"] == "refused"
    assert result["status"] == "completed"


def test_catalog_preserves_leading_zero_and_quoted_newline(tmp_path):
    source = 'Title,SKU,Price\r\n"A\nB",0012,12.50\r\n'
    result = run_inspection("catalog_inspect", {"csv_text": source}, tmp_path)
    assert (tmp_path / "original.csv").read_bytes() == source.encode()
    assert result["summary"]["structurally_safe"] is True
    assert result["summary"]["row_count"] == 1
