from datetime import datetime, timedelta, timezone

import pytest

from repvblicvs_engine.opportunities import CommercialLedger, OpportunityError, qualify_opportunity, review_experiment, valid_source_url


NOW = datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc)  # Friday noon Eastern


def lead(number=0, **changes):
    return {"source_url": f"https://example.com/requests/{number}", "title": f"Clean data request {number}", "source_quote": "Please normalize and validate our CSV export. AI-assisted work is permitted.", "category": "data", "ai_permission": "allowed", "solicited": True, "risk": "low", "capability": "csv_cleanup", **changes}


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://", "example.com/request", "https://localhost/job", "https://127.0.0.1/job", "https://example.com:abc/job", "https://name:password" + "@" + "example.com/job", "https://example.com/ job", "https://example.com/#token", "https://evil..com/job"])
def test_malformed_or_private_source_urls_rejected(url):
    assert not valid_source_url(url)


def test_qualification_does_not_infer_ai_eligibility():
    assert qualify_opportunity(lead(), {"csv_cleanup"})["qualified"]
    assert not qualify_opportunity(lead(ai_permission="unknown"), {"csv_cleanup"})["qualified"]
    assert not qualify_opportunity(lead(ai_permission="prohibited"), {"csv_cleanup"})["qualified"]
    assert not qualify_opportunity(lead(requires_attestation=True), {"csv_cleanup"})["qualified"]
    assert not qualify_opportunity(lead(risk="medium"), {"csv_cleanup"})["qualified"]
    assert not qualify_opportunity(lead(), {"document_package"})["qualified"]


def test_reserved_and_uncertain_contacts_consume_cap_and_replay_is_idempotent():
    ledger = CommercialLedger()
    identifiers = [ledger.register(lead(i), {"csv_cleanup"})["opportunity_id"] for i in range(4)]
    first = ledger.reserve_contact(identifiers[0], idempotency_key="first", now=NOW)
    ledger.resolve_contact("first", "uncertain")
    assert ledger.reserve_contact(identifiers[0], idempotency_key="first", now=NOW) is first
    ledger.reserve_contact(identifiers[1], idempotency_key="second", now=NOW)
    ledger.reserve_contact(identifiers[2], idempotency_key="third", now=NOW)
    with pytest.raises(OpportunityError) as error:
        ledger.reserve_contact(identifiers[3], idempotency_key="fourth", now=NOW)
    assert error.value.code == "daily_contact_cap"
    ledger.resolve_contact("second", "cancelled")
    ledger.reserve_contact(identifiers[3], idempotency_key="fourth", now=NOW)
    restored = CommercialLedger(ledger.export_state())
    assert restored.state == ledger.state


def test_followup_is_once_after_three_business_days():
    ledger = CommercialLedger()
    identifier = ledger.register(lead(), {"csv_cleanup"})["opportunity_id"]
    ledger.reserve_contact(identifier, idempotency_key="initial", now=NOW)
    ledger.resolve_contact("initial", "confirmed", receipt="synthetic-receipt", now=NOW)
    with pytest.raises(OpportunityError) as error:
        ledger.reserve_contact(identifier, kind="followup", idempotency_key="followup", now=NOW + timedelta(days=4))
    assert error.value.code == "followup_too_early"
    ledger.reserve_contact(identifier, kind="followup", idempotency_key="followup", now=NOW + timedelta(days=5))
    with pytest.raises(OpportunityError) as error:
        ledger.reserve_contact(identifier, kind="followup", idempotency_key="again", now=NOW + timedelta(days=8))
    assert error.value.code == "duplicate_contact"


def test_uncertain_contact_needs_reconciliation_before_retry():
    ledger = CommercialLedger()
    identifier = ledger.register(lead(), {"csv_cleanup"})["opportunity_id"]
    ledger.reserve_contact(identifier, idempotency_key="initial", now=NOW)
    ledger.resolve_contact("initial", "uncertain")
    with pytest.raises(OpportunityError) as error:
        ledger.resolve_contact("initial", "cancelled")
    assert error.value.code == "reconciliation_required"
    ledger.resolve_contact("initial", "cancelled", receipt="verified-no-send")
    ledger.reserve_contact(identifier, idempotency_key="retry", now=NOW)


def test_reply_and_unsubscribe_suppress_automatic_followup():
    ledger = CommercialLedger()
    identifier = ledger.register(lead(), {"csv_cleanup"})["opportunity_id"]
    ledger.reserve_contact(identifier, idempotency_key="initial", now=NOW)
    ledger.resolve_contact("initial", "confirmed", receipt="synthetic-receipt", now=NOW)
    ledger.record_event(identifier, "reply", idempotency_key="reply", now=NOW)
    with pytest.raises(OpportunityError) as error:
        ledger.reserve_contact(identifier, kind="followup", idempotency_key="followup", now=NOW + timedelta(days=5))
    assert error.value.code == "conversation_active"


def test_financial_receipts_are_idempotent_and_cash_is_separate():
    ledger = CommercialLedger()
    identifier = ledger.register(lead(), {"csv_cleanup"})["opportunity_id"]
    for event, amount in (("quoted", 10000), ("agreed", 10000), ("paid", 10000), ("fee", 500), ("compute_cost", 100)):
        ledger.record_event(identifier, event, amount_cents=amount, idempotency_key=event, now=NOW)
    summary = ledger.financial_summary()
    assert summary["amounts_cents"]["paid"] == 10000
    assert summary["usable_funds_cents"] == 0
    ledger.record_event(identifier, "settled", amount_cents=10000, idempotency_key="settled", now=NOW)
    ledger.record_event(identifier, "funds_available", amount_cents=9400, idempotency_key="available", now=NOW)
    ledger.record_event(identifier, "paid", amount_cents=10000, idempotency_key="paid", now=NOW)
    assert ledger.financial_summary()["usable_funds_cents"] == 9400
    assert ledger.financial_summary()["spending_authorized"] is False
    with pytest.raises(OpportunityError) as error:
        ledger.record_event(identifier, "paid", amount_cents=9000, idempotency_key="paid", now=NOW)
    assert error.value.code == "idempotency_conflict"


def test_three_offer_cap_and_threshold_review():
    ledger = CommercialLedger()
    for identifier in ("data", "documents", "software"):
        ledger.activate_experiment(identifier, identifier + " service", NOW)
    with pytest.raises(OpportunityError) as error:
        ledger.activate_experiment("fourth", "another service", NOW)
    assert error.value.code == "experiment_capacity"
    assert all(result["decision"] == "replace" for result in ledger.review_experiments(NOW + timedelta(days=7)))
    ledger.activate_experiment("fourth", "replacement service", NOW + timedelta(days=7))
    assert review_experiment({"created_at": NOW.isoformat(), "qualified_contacts": 10, "replies": 1}, NOW)["decision"] == "adjust"
    assert review_experiment({"created_at": NOW.isoformat(), "qualified_contacts": 10, "cleared_net_cents": 1}, NOW)["decision"] == "continue"


def test_successful_experiment_review_opens_a_new_window():
    ledger = CommercialLedger()
    ledger.activate_experiment("data", "data repair", NOW)
    identifier = ledger.register(lead(), {"csv_cleanup"})["opportunity_id"]
    ledger.record_event(identifier, "settled", amount_cents=100, idempotency_key="settled", experiment_id="data", now=NOW)
    assert ledger.review_experiments(NOW + timedelta(days=7))[0]["due"]
    assert not ledger.review_experiments(NOW + timedelta(days=7, seconds=1))[0]["due"]
