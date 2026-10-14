from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import pytest
from repvblicvs_engine.commercial import Commerce
from repvblicvs_engine.operator import ConnectorBridge
from repvblicvs_engine.store import Store
from test_commercial import route_evidence


def fixture(tmp_path):
    store = Store(tmp_path / 'state')
    commerce = Commerce(store)
    now = datetime.now(timezone.utc).isoformat()
    result = commerce.register_opportunity(
        {'source_url': 'https://example.org/request', 'title': 'Synthetic CSV work',
         'source_quote': 'Please repair a CSV; AI-assisted delivery is permitted.',
         'category': 'data', 'ai_permission': 'allowed', 'solicited': True,
         'risk': 'low', 'capability': 'csv_cleanup', 'customer_reference': 'synthetic-customer'},
        {'deliverables': ['CSV and replay script'], 'acceptance': ['Replayed totals'], 'estimated_minutes': 30},
        {'source_read_receipt': 'synthetic-read', 'scope_receipt': 'synthetic-scope',
         'ai_permission_receipt': 'synthetic-terms', 'currently_open': True, 'checked_at': now,
         'route_preflight': route_evidence(datetime.fromisoformat(now))},
        {'csv_cleanup'})
    action = commerce.prepare_contact(result['opportunity_id'], 'synthetic-contact')
    return store, action


def test_one_connector_payload_under_concurrent_frontends(tmp_path):
    store, action = fixture(tmp_path)
    bridge = ConnectorBridge(store)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: bridge.begin(action['id'], 'synthetic-desktop'), range(5)))
    assert sum(r['execute'] for r in results) == 1
    issued = next(r for r in results if r['execute'])
    assert issued['payload']['business_identity'] == 'repvblicvs'
    assert not ConnectorBridge(Store(store.root)).begin(action['id'], 'other-frontend')['execute']
    receipt = {'status': 'confirmed', 'external_ref': 'synthetic-message-id', 'evidence': 'synthetic-send-receipt'}
    assert bridge.receipt(action['id'], issued['attempt_token'], receipt)['status'] == 'delivered'
    assert bridge.receipt(action['id'], issued['attempt_token'], receipt)['status'] == 'delivered'
    with pytest.raises(ValueError):
        bridge.receipt(action['id'], issued['attempt_token'], {**receipt, 'external_ref': 'different-message'})


def test_unknown_receipt_preserved_then_confirmed_by_read_without_resend(tmp_path):
    store, action = fixture(tmp_path)
    bridge = ConnectorBridge(store)
    issued = bridge.begin(action['id'], 'synthetic-desktop')
    bridge.receipt(action['id'], issued['attempt_token'], {'status': 'unknown', 'evidence': 'synthetic-timeout'})
    assert not bridge.begin(action['id'], 'synthetic-desktop')['execute']
    result = bridge.receipt(action['id'], issued['attempt_token'], {'status': 'confirmed', 'external_ref': 'synthetic-existing-message', 'evidence': 'synthetic-sent-folder-read'})
    assert result['status'] == 'delivered'
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM connector_receipts').fetchone()[0] == 2
    with pytest.raises(ValueError):
        bridge.receipt(action['id'], 'invalid-token', {'status': 'confirmed', 'external_ref': 'fake', 'evidence': 'fake'})
