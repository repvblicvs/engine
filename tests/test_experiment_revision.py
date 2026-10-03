from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from repvblicvs_engine.commercial import Commerce, CommercialError
from repvblicvs_engine.opportunities import OpportunityError
from repvblicvs_engine.store import ConflictError, Store
from test_commercial import NOW, FakeTransport, establish_agreement, ready


REVIEW_TIME = NOW + timedelta(days=7)
NEW_OFFER = "Repair bounded tabular exports with a repeatable validation report"
NEW_BAND = [18000, 65000]


def revision_evidence(receipt="synthetic-revision-1", **changes):
    return {"receipt": receipt, "kind": "market_feedback", "finding": "Verified customers requested explicit row reconciliation and an offline replay package.", "checked_at": REVIEW_TIME.isoformat(), **changes}


def due_adjustment(tmp_path):
    commerce, identifier = ready(tmp_path)
    commerce.seed_experiments(NOW)
    commerce.record_event(identifier, "reply", idempotency_key="feedback", receipt="synthetic-market-reply", experiment_id="dataset-repair", now=NOW)
    decisions = commerce.review_experiments(REVIEW_TIME)
    assert next(item for item in decisions if item["experiment_id"] == "dataset-repair")["decision"] == "adjust"
    return commerce, identifier


def revise(commerce, evidence=None, **changes):
    values = {"experiment_id": "dataset-repair", "offer": NEW_OFFER, "capability": "csv_cleanup", "price_band_cents": NEW_BAND, "evidence": revision_evidence() if evidence is None else evidence, "now": REVIEW_TIME}
    return commerce.revise_experiment(**{**values, **changes})


def test_due_seven_day_adjustment_preserves_original_offer_review_and_receipt(tmp_path):
    commerce, _ = due_adjustment(tmp_path)
    original = commerce.snapshot()["state"]["experiments"]["dataset-repair"]
    result = revise(commerce)
    assert result["experiment_id"] == "dataset-repair"
    assert result["decision"] == "adjust"
    restored = Commerce(Store(tmp_path / "state"))
    state = restored.snapshot()["state"]
    revised = state["experiments"]["dataset-repair"]
    assert revised["offer"] == NEW_OFFER
    assert revised["price_band_cents"] == NEW_BAND
    assert revised["last_review"] == original["last_review"]
    assert revised["review_history"] == original["review_history"]
    assert revised["original_created_at"] == NOW.isoformat()
    assert revised["review_history"][0]["evaluated_offer"]["created_at"] == NOW.isoformat()
    assert revised["review_history"][0]["evaluated_offer"]["offer"] == original["offer"]
    record = state["experiment_revisions"]["synthetic-revision-1"]
    assert record["before"]["offer"] == original["offer"]
    assert record["before"]["price_band_cents"] == [15000, 60000]
    assert record["review"] == original["last_review"]
    assert revised["revision_history"] == [record]
    with restored.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='commercial_experiment_revised'").fetchone()[0] == 1


def test_exact_revision_replay_is_idempotent_and_conflicting_receipt_is_rejected(tmp_path):
    commerce, _ = due_adjustment(tmp_path)
    result = revise(commerce)
    assert revise(Commerce(Store(tmp_path / "state")), now=REVIEW_TIME + timedelta(days=1)) == result
    with pytest.raises(ConflictError): revise(commerce, price_band_cents=[20000, 70000])
    with pytest.raises(CommercialError) as error: revise(commerce, evidence=revision_evidence("different-revision"))
    assert error.value.code == "review_already_applied"
    assert len(commerce.snapshot()["state"]["experiment_revisions"]) == 1


def test_replacement_requires_due_retired_offer_and_a_fresh_id(tmp_path):
    commerce, _ = ready(tmp_path)
    commerce.seed_experiments(NOW)
    commerce.review_experiments(REVIEW_TIME)
    original = commerce.snapshot()["state"]["experiments"]["dataset-repair"]
    with pytest.raises(CommercialError): revise(commerce)
    with pytest.raises(CommercialError): revise(commerce, replacement_id="software-fix")
    result = revise(commerce, replacement_id="dataset-repair-v2")
    assert result["experiment_id"] == "dataset-repair-v2"
    assert result["decision"] == "replace"
    state = commerce.snapshot()["state"]
    retired = state["experiments"]["dataset-repair"]
    assert retired["status"] == "retired"
    assert retired["offer"] == original["offer"]
    assert retired["last_review"] == original["last_review"]
    assert retired["replacement_experiment_id"] == "dataset-repair-v2"
    assert state["experiments"]["dataset-repair-v2"]["replaces_experiment_id"] == "dataset-repair"
    assert state["experiments"]["dataset-repair-v2"]["original_created_at"] == REVIEW_TIME.isoformat()
    commerce.seed_experiments(REVIEW_TIME)
    assert commerce.snapshot()["state"]["experiments"]["dataset-repair"]["status"] == "retired"


def test_replenishing_three_retired_experiments_uses_three_fresh_ids(tmp_path):
    commerce, _ = ready(tmp_path)
    originals = commerce.seed_experiments(NOW)
    commerce.review_experiments(REVIEW_TIME)
    for index, original in enumerate(originals):
        commerce.revise_experiment(original["id"], f"Revised bounded offer {index}", original["capability"], original["price_band_cents"], revision_evidence(f"replacement-{index}"), replacement_id=f"offer-v2-{index}", now=REVIEW_TIME)
    state = commerce.snapshot()["state"]
    assert sum(item["status"] == "active" for item in state["experiments"].values()) == 3
    assert sum(item["status"] == "retired" for item in state["experiments"].values()) == 3
    assert len(state["experiment_revisions"]) == 3


def test_replacement_obeys_active_capacity_and_failure_does_not_consume_review(tmp_path):
    commerce, _ = ready(tmp_path)
    commerce.seed_experiments(NOW)
    commerce.review_experiments(REVIEW_TIME)
    with commerce.store.connection(write=True) as db:
        ledger = commerce._ledger(db)
        for index in range(3): ledger.activate_experiment(f"already-active-{index}", "Existing active offer", REVIEW_TIME)
        commerce._save(db, ledger)
    with pytest.raises(OpportunityError) as error: revise(commerce, replacement_id="fourth-active")
    assert error.value.code == "experiment_capacity"
    state = commerce.snapshot()["state"]
    assert "fourth-active" not in state["experiments"]
    assert "review_application" not in state["experiments"]["dataset-repair"]
    assert not state.get("experiment_revisions")


def test_concurrent_replacements_of_one_review_cannot_create_two_offers(tmp_path):
    commerce, _ = ready(tmp_path)
    commerce.seed_experiments(NOW)
    commerce.review_experiments(REVIEW_TIME)
    def replacement(index):
        try: return revise(commerce, evidence=revision_evidence(f"concurrent-{index}"), replacement_id=f"concurrent-offer-{index}")
        except CommercialError as error:
            assert error.code == "review_already_applied"
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [result for result in pool.map(replacement, range(2)) if result]
    assert len(results) == 1
    assert sum(item["status"] == "active" for item in commerce.snapshot()["state"]["experiments"].values()) == 1


def test_revision_before_due_review_or_after_profitable_continue_is_rejected(tmp_path):
    commerce, identifier = ready(tmp_path)
    commerce.seed_experiments(NOW)
    with pytest.raises(CommercialError) as error: revise(commerce)
    assert error.value.code == "revision_not_due"
    commerce.record_event(identifier, "settled", amount_cents=20000, idempotency_key="settlement", receipt="synthetic-settlement", experiment_id="dataset-repair", now=NOW)
    commerce.review_experiments(REVIEW_TIME)
    with pytest.raises(CommercialError) as error: revise(commerce)
    assert error.value.code == "revision_not_due"


def test_adjustment_cannot_be_disguised_as_replacement(tmp_path):
    commerce, _ = due_adjustment(tmp_path)
    with pytest.raises(CommercialError): revise(commerce, replacement_id="improper-replacement")
    assert "improper-replacement" not in commerce.snapshot()["state"]["experiments"]


@pytest.mark.parametrize("changes", [
    {"offer": " "}, {"capability": "unverified_image_model"}, {"capability": []},
    {"price_band_cents": [0, 500]}, {"price_band_cents": [True, 500]},
    {"price_band_cents": [600, 500]}, {"price_band_cents": [100, 1_000_001]},
    {"evidence": {}}, {"evidence": revision_evidence(receipt="")},
    {"evidence": revision_evidence(finding=" ")},
    {"evidence": revision_evidence(kind="unsupported")},
    {"evidence": revision_evidence(kind="primary_source")},
    {"evidence": revision_evidence(kind="primary_source", source_url="http://localhost/private")},
    {"evidence": revision_evidence(checked_at=(REVIEW_TIME + timedelta(days=1)).isoformat())},
    {"evidence": revision_evidence(shell="untrusted command")},
])
def test_unbounded_or_unsupported_revision_and_empty_fabricated_evidence_are_rejected(tmp_path, changes):
    commerce, _ = due_adjustment(tmp_path)
    with pytest.raises(OpportunityError): revise(commerce, **changes)
    assert not commerce.snapshot()["state"].get("experiment_revisions")


def test_primary_source_evidence_and_review_history_remain_available_after_next_review(tmp_path):
    commerce, identifier = due_adjustment(tmp_path)
    actual_source = revision_evidence(kind="primary_source", source_url="https://example.com/current-work-request")
    revise(commerce, evidence=actual_source)
    later = REVIEW_TIME + timedelta(days=7)
    commerce.record_event(identifier, "settled", amount_cents=10000, idempotency_key="settled", receipt="synthetic-settlement", experiment_id="dataset-repair", now=later)
    commerce.review_experiments(later)
    experiment = commerce.snapshot()["state"]["experiments"]["dataset-repair"]
    assert [item["decision"] for item in experiment["review_history"]] == ["adjust", "continue"]
    assert experiment["revision_history"][0]["request"]["evidence"]["source_url"] == "https://example.com/current-work-request"


def test_offer_revision_does_not_reprice_existing_quotes_or_customer_agreements(tmp_path):
    commerce, identifier = ready(tmp_path)
    commerce.seed_experiments(NOW)
    commerce.prepare_contact(identifier, "initial", experiment_id="dataset-repair", now=NOW)
    quote = establish_agreement(commerce, identifier, FakeTransport())
    agreement = commerce.snapshot()["state"]["agreements"][identifier]
    original_actions = commerce.store.list_actions()
    commerce.review_experiments(REVIEW_TIME)
    revise(commerce)
    assert commerce.snapshot()["state"]["agreements"][identifier] == agreement
    assert commerce.store.list_actions() == original_actions
    unchanged_quote = next(action for action in commerce.store.list_actions() if action["id"] == quote["id"])
    assert unchanged_quote["payload"]["price_cents"] == 20000
