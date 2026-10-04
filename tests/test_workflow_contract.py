import pytest
from repvblicvs_engine.inspection import run_inspection
from repvblicvs_engine.workflows import run_workflow, WorkflowError


@pytest.mark.parametrize('kind,payload', [
    ('document_package', {'title': 'Example', 'content': 'Example', 'character': {'species': 'fox'}}),
    ('csv_cleanup', {'csv_text': 'a\n1\n', 'acceptance': {'invented_check': True}}),
    ('document_package', {'content': 'Example', 'illustration': {'species': 'fox', 'animated': True}}),
])
def test_unsupported_customer_requests_cannot_be_silently_ignored(tmp_path, kind, payload):
    with pytest.raises(WorkflowError) as error:
        run_workflow(kind, payload, tmp_path / 'output')
    assert error.value.code == 'unsupported_field'
    assert not (tmp_path / 'output').exists()


@pytest.mark.parametrize("runner", [run_workflow, run_inspection])
@pytest.mark.parametrize("kind,payload", [
    ("json_preflight", {"text": '{"a":1}', "acceptance": {"required_keys": ["b"]}}),
    ("json_preflight", {"text": '{"a":1}', "mode": "update"}),
    ("catalog_inspect", {"csv_text": "Title,Price\nExample,1.00\n", "acceptance": {"expected_rows": 2}}),
    ("catalog_inspect", {"csv_text": "Title,Price\nExample,1.00\n", "variants": True}),
])
def test_inspection_unsupported_requests_fail_before_output_creation(tmp_path, runner, kind, payload):
    output = tmp_path / "output"
    with pytest.raises(WorkflowError) as error:
        runner(kind, payload, output)
    assert error.value.code == "unsupported_field"
    assert not output.exists()


@pytest.mark.parametrize("runner", [run_workflow, run_inspection])
@pytest.mark.parametrize("kind", ["json_preflight", "catalog_inspect"])
@pytest.mark.parametrize("payload", [{}, {"text": "first", "csv_text": "second"}, {"text": None}])
def test_inspection_source_aliases_require_one_text_input(tmp_path, runner, kind, payload):
    output = tmp_path / "output"
    with pytest.raises(WorkflowError) as error:
        runner(kind, payload, output)
    assert error.value.code == "invalid_input"
    assert not output.exists()


@pytest.mark.parametrize("runner", [run_workflow, run_inspection])
@pytest.mark.parametrize("options", [{"mode": "delete"}, {"header_style": "invented"}, {"mode": []}])
def test_inspection_catalog_options_are_validated_before_output(tmp_path, runner, options):
    output = tmp_path / "output"
    with pytest.raises(WorkflowError) as error:
        runner("catalog_inspect", {"csv_text": "Title,Price\nExample,1.00\n", **options}, output)
    assert error.value.code == "invalid_input"
    assert not output.exists()


@pytest.mark.parametrize("runner", [run_workflow, run_inspection])
@pytest.mark.parametrize("kind,source,options", [
    ("json_preflight", '{"catalog":{"price":12,"price":13}}', {}),
    ("catalog_inspect", "Title,Handle,Variant Price\r\nExample,example,1.00\r\n", {"mode": "update", "header_style": "legacy"}),
])
@pytest.mark.parametrize("alias", ["text", "csv_text"])
def test_inspection_supported_fields_preserve_original_and_honest_status(tmp_path, runner, kind, source, options, alias):
    output = tmp_path / "output"
    result = runner(kind, {alias: source, **options}, output)
    suffix = "json" if kind == "json_preflight" else "csv"
    assert (output / f"original.{suffix}").read_bytes() == source.encode("utf-8")
    assert result["status"] == "completed"
    if kind == "json_preflight":
        assert result["summary"]["status"] == "refused"
        assert result["summary"]["duplicate_count"] == 1
    else:
        assert result["summary"]["profile"] == {"mode": "update", "header_style": "legacy", "variants": False}
        assert result["summary"]["structurally_safe"] is True
