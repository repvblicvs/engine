"""Behavioral checks for participation costs and stage-specific enrollment."""
from datetime import datetime, timedelta, timezone

import pytest

from repvblicvs_engine.commercial import Commerce, CommercialError
from repvblicvs_engine.operator import ConnectorBridge
from repvblicvs_engine.route_preflight import assess_route
from repvblicvs_engine.store import Store, encode
from test_application import APPLICATION
from test_commercial import NOW, SCOPE, FakeTransport, evidence, route_evidence, source
from test_inquiry import inquiry_evidence, request


def application(commerce, identifier):
    return commerce.prepare_application(identifier, "proposal", now=NOW, **APPLICATION)


@pytest.mark.parametrize("route", [None, {}, route_evidence(application_fee_cents=None),
    route_evidence(deposit_cents=10000), route_evidence(required_purchase_cents=100),
    route_evidence(submission_steps=[]), route_evidence(payout_prerequisites=None),
    route_evidence(checked_at=(NOW + timedelta(seconds=1)).isoformat()),
    route_evidence(checked_at=(NOW - timedelta(days=2)).isoformat())])
def test_unverified_or_paid_access_cannot_prepare_application_or_qualify(tmp_path, route):
    commerce = Commerce(Store(tmp_path / "state"))
    inquiry = commerce.register_solicitation(request(), inquiry_evidence(route_preflight=route), now=NOW)
    assert inquiry["inquiry_eligible"]  # A free permitted clarification remains possible.
    assert not inquiry["proposal_ready"]
    with pytest.raises(CommercialError, match="Application route"):
        application(commerce, inquiry["opportunity_id"])
    assert commerce.store.list_actions() == []
    qualified = commerce.register_opportunity(source(1), SCOPE, evidence(route_preflight=route), {"csv_cleanup"}, now=NOW)
    assert not qualified["qualified"]


def test_cost_unknown_is_not_free_and_does_not_block_other_routes(tmp_path):
    commerce = Commerce(Store(tmp_path / "state"))
    unknown = commerce.register_solicitation(request(), inquiry_evidence(route_preflight=None), now=NOW)
    assert not unknown["route"]["free_to_participate"]
    available = commerce.register_solicitation(request(1), inquiry_evidence(), now=NOW)
    assert available["proposal_ready"]
    assert application(commerce, available["opportunity_id"])["status"] == "prepared"


@pytest.mark.parametrize("stage,proposal_ready,execution_ready", [
    ("before_submission", False, False), ("before_execution", True, False),
    ("before_payment", True, True)])
def test_enrollment_only_blocks_the_stage_that_requires_it(tmp_path, stage, proposal_ready, execution_ready):
    route = route_evidence(payout_ready=False, payout_required_at=stage,
                           payout_prerequisites=["Enroll a receiving account before the stated stage"])
    assessment = assess_route({"route_preflight": route}, NOW)
    assert assessment["proposal_ready"] is proposal_ready
    assert assessment["execution_ready"] is execution_ready
    assert not assessment["payment_ready"]
    commerce = Commerce(Store(tmp_path / "state"))
    inquiry = commerce.register_solicitation(request(), inquiry_evidence(route_preflight=route), now=NOW)
    if proposal_ready:
        assert application(commerce, inquiry["opportunity_id"])["status"] == "prepared"
    else:
        with pytest.raises(CommercialError): application(commerce, inquiry["opportunity_id"])
    qualified = commerce.register_opportunity(source(1), SCOPE, evidence(route_preflight=route), {"csv_cleanup"}, now=NOW)
    assert qualified["qualified"] is execution_ready


def test_ready_usdc_receipt_does_not_require_bank_withdrawal():
    result = assess_route({"route_preflight": route_evidence(currency="USDC",
        payout_prerequisites=["Verified USDC receiving address on the specified network"])}, NOW)
    assert result["proposal_ready"] and result["execution_ready"] and result["payment_ready"]


def test_new_fee_discovered_after_preparation_blocks_connector_callback(tmp_path):
    commerce = Commerce(Store(tmp_path / "state"))
    inquiry = commerce.register_solicitation(request(), inquiry_evidence(), now=NOW)
    action = application(commerce, inquiry["opportunity_id"])
    commerce.register_solicitation(request(), inquiry_evidence(route_preflight=route_evidence(deposit_cents=10000)), now=NOW)
    transport = FakeTransport()
    with pytest.raises(CommercialError, match="Refresh application"):
        commerce.dispatch(action["id"], transport, now=NOW)
    assert not transport.calls
    assert commerce.store.list_actions()[0]["status"] == "prepared"


def test_legacy_qualified_record_requires_route_refresh_before_new_action(tmp_path):
    commerce = Commerce(Store(tmp_path / "state"))
    identifier = commerce.register_opportunity(source(), SCOPE, evidence(), {"csv_cleanup"}, now=NOW)["opportunity_id"]
    with commerce.store.connection(write=True) as db:
        ledger = commerce._ledger(db)
        ledger.state["opportunities"][identifier]["evidence"].pop("route_preflight")
        ledger.state["opportunities"][identifier].pop("route_preflight")
        commerce._save(db, ledger)
    with pytest.raises(CommercialError, match="Verify participation costs"):
        commerce.prepare_contact(identifier, "legacy-contact", now=NOW)
    assert commerce.store.list_actions() == []


@pytest.mark.parametrize("route", [None, route_evidence(deposit_cents=10000),
    route_evidence(payout_ready=False, payout_required_at="before_submission",
                   payout_prerequisites=["Receiving enrollment required before applying"])])
def test_opportunity_refresh_supersedes_older_inquiry_route_before_dispatch(tmp_path, route):
    commerce = Commerce(Store(tmp_path / "state"))
    identifier = commerce.register_solicitation(request(), inquiry_evidence(), now=NOW)["opportunity_id"]
    action = application(commerce, identifier)
    updated = commerce.register_opportunity(source(), SCOPE, evidence(route_preflight=route), {"csv_cleanup"}, now=NOW)
    assert not updated["qualified"]
    transport = FakeTransport()
    with pytest.raises(CommercialError) as error:
        commerce.dispatch(action["id"], transport, now=NOW)
    assert error.value.code == "route_not_ready"
    assert not transport.calls
    assert commerce.store.list_actions()[0]["status"] == "prepared"


@pytest.mark.parametrize("changes", [
    {"submission_url": "https://example.com/new-portal"},
    {"submission_steps": ["Submit through the new reviewed portal"]},
    {"submission_receipt": "synthetic-updated-submission-review"},
    {"cost_receipt": "synthetic-updated-cost-review"},
    {"currency": "USDC"},
    {"payout_prerequisites": ["Receiving account verified on the updated network"]},
    {"payout_required_at": "before_payment"},
    {"payout_requirements_receipt": "synthetic-updated-payout-review"},
])
@pytest.mark.parametrize("refresh", ["solicitation", "opportunity"])
def test_changed_free_route_requires_new_application_review_before_dispatch(tmp_path, changes, refresh):
    commerce = Commerce(Store(tmp_path / "state"))
    identifier = commerce.register_solicitation(request(), inquiry_evidence(), now=NOW)["opportunity_id"]
    action = application(commerce, identifier)
    assert action["payload"]["submission_route"]["submission_url"] == "https://example.com/submit"
    assert len(action["payload"]["submission_route_hash"]) == 64
    route = route_evidence(**changes)
    assert assess_route({"route_preflight": route}, NOW)["proposal_ready"]
    if refresh == "solicitation":
        commerce.register_solicitation(request(), inquiry_evidence(route_preflight=route), now=NOW)
    else:
        commerce.register_opportunity(source(), SCOPE, evidence(route_preflight=route), {"csv_cleanup"}, now=NOW)
    transport = FakeTransport()
    with pytest.raises(CommercialError) as error:
        commerce.dispatch(action["id"], transport, now=NOW)
    assert error.value.code == "submission_route_changed"
    assert not transport.calls
    assert commerce.store.list_actions()[0]["status"] == "prepared"


def test_freshness_only_route_refresh_preserves_reviewed_application(tmp_path):
    commerce = Commerce(Store(tmp_path / "state"))
    identifier = commerce.register_solicitation(request(), inquiry_evidence(), now=NOW)["opportunity_id"]
    action = application(commerce, identifier)
    later = NOW + timedelta(hours=1)
    commerce.register_solicitation(request(), inquiry_evidence(later), now=later)
    transport = FakeTransport()
    assert commerce.dispatch(action["id"], transport, now=later)["status"] == "delivered"
    assert len(transport.calls) == 1


def test_legacy_prepared_application_requires_bound_route_review(tmp_path):
    commerce = Commerce(Store(tmp_path / "state"))
    identifier = commerce.register_solicitation(request(), inquiry_evidence(), now=NOW)["opportunity_id"]
    action = application(commerce, identifier)
    action["payload"].pop("submission_route")
    action["payload"].pop("submission_route_hash")
    with commerce.store.connection(write=True) as db:
        db.execute("UPDATE outbox SET payload=? WHERE id=?", (encode(action["payload"]), action["id"]))
    transport = FakeTransport()
    with pytest.raises(CommercialError) as error:
        commerce.dispatch(action["id"], transport, now=NOW)
    assert error.value.code == "submission_route_changed"
    assert not transport.calls


def test_connector_begin_does_not_release_payload_for_changed_route(tmp_path):
    now = datetime.now(timezone.utc)
    store = Store(tmp_path / "state")
    commerce = Commerce(store)
    identifier = commerce.register_solicitation(request(), inquiry_evidence(now), now=now)["opportunity_id"]
    action = commerce.prepare_application(identifier, "application", now=now, **APPLICATION)
    commerce.register_opportunity(source(), SCOPE,
        evidence(now, route_preflight=route_evidence(now, submission_url="https://example.com/new-portal")),
        {"csv_cleanup"}, now=now)
    with pytest.raises(CommercialError) as error:
        ConnectorBridge(store).begin(action["id"], "synthetic-operator")
    assert error.value.code == "submission_route_changed"
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM connector_attempts").fetchone()[0] == 0
    assert store.list_actions()[0]["status"] == "prepared"


@pytest.mark.parametrize("changes", [
    {"submission_url": "https://example.com/new-portal"},
    {"submission_steps": ["Send through the newly reviewed portal"]},
    {"submission_receipt": "synthetic-new-submission-review"},
])
def test_qualified_contact_is_bound_to_its_reviewed_submission_route(tmp_path, changes):
    commerce = Commerce(Store(tmp_path / "state"))
    identifier = commerce.register_opportunity(source(), SCOPE, evidence(), {"csv_cleanup"}, now=NOW)["opportunity_id"]
    action = commerce.prepare_contact(identifier, "contact", now=NOW)
    assert action["payload"]["submission_route"]["submission_url"] == "https://example.com/submit"
    updated = commerce.register_opportunity(source(), SCOPE,
        evidence(route_preflight=route_evidence(**changes)), {"csv_cleanup"}, now=NOW)
    assert updated["qualified"]
    transport = FakeTransport()
    with pytest.raises(CommercialError) as error:
        commerce.dispatch(action["id"], transport, now=NOW)
    assert error.value.code == "submission_route_changed"
    assert not transport.calls
    assert commerce.store.list_actions()[0]["status"] == "prepared"


def test_new_application_cannot_use_old_inquiry_route_after_paid_opportunity_refresh(tmp_path):
    commerce = Commerce(Store(tmp_path / "state"))
    identifier = commerce.register_solicitation(request(), inquiry_evidence(), now=NOW)["opportunity_id"]
    commerce.register_opportunity(source(), SCOPE,
        evidence(route_preflight=route_evidence(deposit_cents=10000)), {"csv_cleanup"}, now=NOW)
    with pytest.raises(CommercialError) as error:
        application(commerce, identifier)
    assert error.value.code == "route_not_ready"
    assert commerce.store.list_actions() == []


def test_permitted_inquiry_can_clarify_missing_route_without_claiming_readiness(tmp_path):
    commerce = Commerce(Store(tmp_path / "state"))
    result = commerce.register_solicitation(request(), inquiry_evidence(route_preflight=None), now=NOW)
    assert not result["proposal_ready"]
    action = commerce.prepare_inquiry(result["opportunity_id"], "clarify", now=NOW)
    transport = FakeTransport()
    assert commerce.dispatch(action["id"], transport, now=NOW)["status"] == "delivered"
    assert len(transport.calls) == 1


@pytest.mark.parametrize("target,receipt,code", [
    ("different-customer", "synthetic-official-public-contact", "source_target_changed"),
    (request()["customer_reference"], "synthetic-new-contact-review", "contact_permission_changed"),
])
def test_inquiry_rechecks_the_reviewed_contact_target_and_permission(tmp_path, target, receipt, code):
    commerce = Commerce(Store(tmp_path / "state"))
    identifier = commerce.register_solicitation(request(), inquiry_evidence(), now=NOW)["opportunity_id"]
    action = commerce.prepare_inquiry(identifier, "clarify", now=NOW)
    commerce.register_solicitation(request(customer_reference=target),
        inquiry_evidence(contact_permission_receipt=receipt), now=NOW)
    transport = FakeTransport()
    with pytest.raises(CommercialError) as error:
        commerce.dispatch(action["id"], transport, now=NOW)
    assert error.value.code == code
    assert not transport.calls
