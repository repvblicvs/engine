import pytest
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
