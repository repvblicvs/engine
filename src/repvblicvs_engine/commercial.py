"""Durable business policy over the engine's one private SQLite store.

Connector callbacks are supplied by a trusted operator. Intake text is data:
this module has no shell, model, credential, or network execution facility.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Protocol

from .opportunities import CommercialLedger, Opportunity, OpportunityError, _utc, contact_policy, qualify_inquiry, valid_source_url
from .route_preflight import COST_FIELDS, assess_route
from .store import ConflictError, Store, encode


DEFAULT_EXPERIMENTS = (
    {"id": "dataset-repair", "offer": "Dataset repair and repeatable automation", "price_band_cents": [15000, 60000], "capability": "csv_cleanup"},
    {"id": "software-fix", "offer": "One reproducible software fix with a regression test", "price_band_cents": [20000, 90000], "capability": "software_fix"},
    {"id": "sourced-document", "offer": "Sourced document, report, or presentation", "price_band_cents": [10000, 50000], "capability": "document_package"},
)
PRIORITIES = {"customer_delivery": 100, "acquisition": 80, "revenue_product": 60, "maintenance": 40, "exploratory_research": 0}
INQUIRY_QUESTIONS = {
    "funding": "Is a fixed paid scope currently funded, and what is its budget and currency?",
    "availability": "Is that scope still available without duplicating an assigned or completed contribution?",
    "ai_eligibility": "Does it accept an AI-operated business with AI-assisted implementation and validation, without an independent human technical-review claim?",
    "scope": "What exact deliverables and requirements are wanted?",
    "acceptance": "What measurable acceptance criteria and review process apply?",
    "assignment": "Must an assignment or scope approval be confirmed before work begins?",
    "payment": "What payment method, prerequisites, fees and expected review or payout timing apply?",
}
EXPERIMENT_CAPABILITIES = {"csv_cleanup", "document_package", "software_fix"}


class Transport(Protocol):
    """Callbacks resolve connector details privately; no guessed API keys."""

    def send(self, payload: dict, *, idempotency_key: str) -> dict: ...
    def invoice(self, payload: dict, *, idempotency_key: str) -> dict: ...
    def receipt(self, action: dict) -> dict: ...
    def search(self, query: dict) -> list[dict]: ...
    def read(self, reference: str) -> dict: ...


class CommercialError(OpportunityError):
    pass


def _scope(value: dict) -> dict:
    allowed = {"deliverables", "acceptance", "estimated_minutes", "exclusions"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise CommercialError("invalid_scope", "Scope supports deliverables, acceptance, estimated_minutes, and exclusions only.")
    result = {}
    for name in ("deliverables", "acceptance", "exclusions"):
        items = value.get(name, [])
        if not isinstance(items, list) or len(items) > 20 or any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in items):
            raise CommercialError("invalid_scope", "Scope lists must contain bounded nonempty text.")
        result[name] = [item.strip() for item in items]
    if not result["deliverables"] or not result["acceptance"]:
        raise CommercialError("invalid_scope", "Verified scope must state deliverables and measurable acceptance.")
    minutes = value.get("estimated_minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 2400:
        raise CommercialError("invalid_scope", "Estimated effort must be 1–2400 minutes.")
    result["estimated_minutes"] = minutes
    return result


def _amount(cents: int, currency: str) -> None:
    if isinstance(cents, bool) or not isinstance(cents, int) or not 0 < cents <= 1_000_000 or currency != "USD":
        raise CommercialError("invalid_price", "A bounded quote requires positive integer USD cents up to 10,000 dollars.")


def _revision_evidence(value: dict, current: datetime) -> dict:
    allowed = {"receipt", "kind", "finding", "checked_at", "source_url"}
    if not isinstance(value, dict) or set(value) - allowed or not isinstance(value.get("kind"), str) or value["kind"] not in {"primary_source", "market_feedback", "delivery_results"}:
        raise CommercialError("invalid_revision_evidence", "Revision needs trusted primary-source, market-feedback or delivery-result evidence.")
    result = {"kind": value["kind"]}
    for key, limit in (("receipt", 512), ("finding", 2000)):
        item = value.get(key)
        if not isinstance(item, str) or not item.strip() or len(item) > limit:
            raise CommercialError("invalid_revision_evidence", "Revision evidence needs a bounded receipt and substantive finding.")
        result[key] = item.strip()
    if not value.get("checked_at"):
        raise CommercialError("invalid_revision_evidence", "Revision evidence needs a verified timezone-aware checked_at.")
    checked = _utc(value["checked_at"])
    if checked > current: raise CommercialError("invalid_revision_evidence", "Future evidence cannot justify a present revision.")
    result["checked_at"] = checked.isoformat()
    source_url = value.get("source_url")
    if source_url is not None or value["kind"] == "primary_source":
        if not valid_source_url(source_url): raise CommercialError("invalid_revision_evidence", "Primary evidence needs a valid public source URL; private feedback may use its receipt.")
        result["source_url"] = source_url
    return result


def _action(row) -> dict:
    if row is None: raise KeyError("Commercial action not found")
    return dict(row) | {"payload": json.loads(row["payload"])}


def _submission_route(entry: dict) -> dict:
    """Bind reviewed route content while permitting a freshness-only refresh."""
    route = entry.get("route_preflight", {})
    keys = (*COST_FIELDS, "currency", "cost_receipt", "submission_url", "submission_steps",
            "submission_receipt", "payout_prerequisites", "payout_required_at",
            "payout_ready", "payout_requirements_receipt")
    return json.loads(encode({key: route.get(key) for key in keys}))


def _route_hash(route: dict) -> str:
    return hashlib.sha256(encode(route).encode()).hexdigest()


class Commerce:
    def __init__(self, store: Store):
        self.store = store
        with store.connection(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS commerce_execution(
                    task_id TEXT PRIMARY KEY, classification TEXT NOT NULL,
                    reserved_units REAL NOT NULL, actual_units REAL,
                    status TEXT NOT NULL, created REAL NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES('commerce',?)", (encode({}),))

    @staticmethod
    def _ledger(db) -> CommercialLedger:
        row = db.execute("SELECT value FROM meta WHERE key='commerce'").fetchone()
        return CommercialLedger(json.loads(row[0]))

    @staticmethod
    def _save(db, ledger: CommercialLedger) -> None:
        db.execute("UPDATE meta SET value=? WHERE key='commerce'", (encode(ledger.state),))

    def snapshot(self) -> dict:
        with self.store.connection() as db:
            ledger = self._ledger(db)
            execution = [dict(row) for row in db.execute("SELECT * FROM commerce_execution ORDER BY created")]
            return {"state": ledger.export_state(), "financial": ledger.financial_summary(), "execution": execution, "transport": "supplied_by_trusted_operator"}

    def set_contact_policy(self, *, daily_contact_limit: int | None, authorization_receipt: str, max_dispatch_batch: int = 20, now: datetime | str | None = None) -> dict:
        """Record a trusted owner/operator instruction, never source-supplied data.

        Null removes the global daily cap. Customer suppression, solicitation
        qualification, deduplication and follow-up eligibility remain unchanged.
        """
        policy = contact_policy({"daily_contact_limit": daily_contact_limit, "max_dispatch_batch": max_dispatch_batch})
        if not isinstance(authorization_receipt, str) or not authorization_receipt.strip() or len(authorization_receipt) > 512:
            raise CommercialError("invalid_contact_policy", "An explicit bounded owner authorization receipt is required.")
        receipt = authorization_receipt.strip()
        current = _utc(now)
        with self.store.connection(write=True) as db:
            ledger = self._ledger(db)
            previous = ledger.state.get("contact_policy")
            if previous is not None and contact_policy(previous) == policy and previous.get("authorization_receipt") == receipt:
                return dict(previous)
            result = {**policy, "authorization_receipt": receipt, "updated_at": current.isoformat()}
            ledger.state["contact_policy"] = result
            ledger.state.setdefault("contact_policy_history", []).append(dict(result))
            self._save(db, ledger)
            Store._event(db, "commercial_contact_policy_changed", **result)
            return result

    def seed_experiments(self, now: datetime | str | None = None) -> list[dict]:
        with self.store.connection(write=True) as db:
            ledger = self._ledger(db)
            items = []
            for default in DEFAULT_EXPERIMENTS:
                item = ledger.activate_experiment(default["id"], default["offer"], now)
                item.setdefault("price_band_cents", default["price_band_cents"])
                item.setdefault("capability", default["capability"])
                item.setdefault("original_created_at", item["created_at"])
                items.append(item)
            self._save(db, ledger)
            return items

    def revise_experiment(self, experiment_id: str, offer: str, capability: str, price_band_cents: list[int], evidence: dict, replacement_id: str | None = None, *, now: datetime | str | None = None) -> dict:
        """Apply one due review using actual evidence, preserving customer terms.

        The evidence receipt is the immutable revision idempotency key. This
        trusted-operator operation cannot be invoked by customer intake text.
        """
        if not isinstance(offer, str) or not offer.strip() or len(offer) > 1000 or not isinstance(capability, str) or capability not in EXPERIMENT_CAPABILITIES:
            raise CommercialError("invalid_experiment_revision", "Revision requires a bounded offer and a supported delivery capability.")
        if not isinstance(price_band_cents, list) or len(price_band_cents) != 2:
            raise CommercialError("invalid_price_band", "An advisory price band contains minimum and maximum integer USD cents.")
        for price in price_band_cents: _amount(price, "USD")
        if price_band_cents[0] > price_band_cents[1]: raise CommercialError("invalid_price_band", "Minimum offer price cannot exceed maximum.")
        for identifier in (experiment_id, replacement_id):
            if identifier is not None and (not isinstance(identifier, str) or not identifier or len(identifier) > 128 or any(not (char.isascii() and (char.isalnum() or char in "-_")) for char in identifier)):
                raise CommercialError("invalid_experiment_revision", "Experiment identifiers use bounded ASCII letters, digits, hyphens or underscores.")
        if experiment_id is None: raise CommercialError("invalid_experiment_revision", "An existing reviewed experiment is required.")
        current = _utc(now)
        verified = _revision_evidence(evidence, current)
        proposed = {"experiment_id": experiment_id, "offer": offer.strip(), "capability": capability, "price_band_cents": list(price_band_cents), "evidence": verified, "replacement_id": replacement_id}
        with self.store.connection(write=True) as db:
            ledger = self._ledger(db)
            revisions = ledger.state.setdefault("experiment_revisions", {})
            existing = revisions.get(verified["receipt"])
            if existing:
                if existing["request"] != proposed: raise ConflictError("Experiment revision receipt reused for different parameters")
                return existing["result"]
            original = ledger.state["experiments"].get(experiment_id)
            if not original: raise CommercialError("unknown_experiment", "Revision requires an existing reviewed experiment.")
            review = original.get("last_review", {})
            if review.get("due") is not True or review.get("decision") not in {"adjust", "replace"}:
                raise CommercialError("revision_not_due", "Only a due adjustment or replacement review permits revision.")
            review_at = review.get("at")
            if not review_at or _utc(review_at) > current:
                raise CommercialError("revision_not_due", "A completed review must precede its revision.")
            if original.get("review_application", {}).get("review_at") == review_at:
                raise CommercialError("review_already_applied", "This review already has an accountable revision; wait for the next due review.")
            before = json.loads(encode({key: original[key] for key in ("id", "offer", "capability", "price_band_cents", "status", "created_at", "last_review", "reviewed_contacts") if key in original}))
            if review["decision"] == "adjust":
                if original["status"] != "active" or replacement_id is not None:
                    raise CommercialError("invalid_experiment_revision", "A due adjustment revises its active offer without a replacement id.")
                target = original
                target["created_at"] = current.isoformat()
                target["reviewed_contacts"] = sum(item["kind"] == "initial" and item["state"] == "confirmed" and not item.get("qualification_only", False) and item.get("experiment_id") == experiment_id for item in ledger.state["outreach"])
            else:
                if original["status"] != "retired" or not replacement_id or replacement_id in ledger.state["experiments"]:
                    raise CommercialError("invalid_experiment_revision", "A retired replacement review needs a fresh experiment identifier.")
                target = ledger.activate_experiment(replacement_id, proposed["offer"], current)
                target["original_created_at"] = target["created_at"]
                target["replaces_experiment_id"] = experiment_id
                original["replacement_experiment_id"] = replacement_id
            target.update({"offer": proposed["offer"], "capability": capability, "price_band_cents": list(price_band_cents), "revision_receipt": verified["receipt"]})
            application = {"review_at": review_at, "revision_receipt": verified["receipt"], "target_experiment_id": target["id"], "at": current.isoformat()}
            original["review_application"] = application
            result = {"experiment_id": target["id"], "original_experiment_id": experiment_id, "decision": review["decision"], "status": target["status"], "offer": target["offer"], "capability": capability, "price_band_cents": list(price_band_cents), "revision_receipt": verified["receipt"], "at": current.isoformat()}
            record = {"request": proposed, "result": result, "before": before, "review": json.loads(encode(review))}
            revisions[verified["receipt"]] = record
            original.setdefault("revision_history", []).append(json.loads(encode(record)))
            if target is not original: target["revision_history"] = [json.loads(encode(record))]
            self._save(db, ledger)
            Store._event(db, "commercial_experiment_revised", experiment_id=target["id"], original_experiment_id=experiment_id, decision=review["decision"], revision_receipt=verified["receipt"])
            return result

    def register_opportunity(self, opportunity: dict, scope: dict, evidence: dict, available_capabilities: set[str], *, now: datetime | str | None = None) -> dict:
        """Evidence is a trusted source-read receipt, not fields inferred from text."""
        candidate = Opportunity.from_dict(opportunity)
        normalized = _scope(scope)
        current = _utc(now)
        if not isinstance(evidence, dict): raise CommercialError("invalid_evidence", "Evidence must be a verified operator record.")
        reasons = []
        for key in ("source_read_receipt", "scope_receipt", "ai_permission_receipt"):
            if not isinstance(evidence.get(key), str) or not evidence[key].strip(): reasons.append(key + "_missing")
        if evidence.get("currently_open") is not True: reasons.append("request_not_verified_open")
        checked = evidence.get("checked_at")
        if not checked: reasons.append("freshness_missing")
        else:
            age = (current - _utc(checked)).total_seconds()
            if age < 0: reasons.append("source_evidence_future")
            elif age > 86400: reasons.append("source_evidence_stale")
        route = assess_route(evidence, current)
        if not route["execution_ready"]: reasons.extend(route["reasons"])
        with self.store.connection(write=True) as db:
            ledger = self._ledger(db)
            previous = ledger.state["opportunities"].get(candidate.id, {})
            scope_hash = hashlib.sha256(encode(normalized).encode()).hexdigest()
            if previous.get("scope_hash") and previous["scope_hash"] != scope_hash:
                raise CommercialError("scope_changed", "Revised scope needs a new explicit commercial record; old customer agreement must remain intact.")
            assessment = ledger.register(candidate, available_capabilities)
            assessment["reasons"].extend(reasons)
            assessment["qualified"] = not assessment["reasons"]
            assessment["score"] = 100 if assessment["qualified"] else 0
            assessment["route"] = route
            entry = ledger.state["opportunities"][candidate.id]
            entry.update({"scope": normalized, "scope_hash": scope_hash, "evidence": evidence,
                          "route_preflight": evidence.get("route_preflight")})
            if previous.get("inquiry_evidence"): entry["inquiry_evidence"] = previous["inquiry_evidence"]
            if previous.get("experiment_id"): entry["experiment_id"] = previous["experiment_id"]
            if assessment["qualified"]:
                ledger.record_event(candidate.id, "qualified", idempotency_key="qualified:" + candidate.id, now=current)
            self._save(db, ledger)
            Store._event(db, "commercial_opportunity_assessed", opportunity_id=candidate.id, qualified=assessment["qualified"], reasons=assessment["reasons"])
            return assessment

    def register_solicitation(self, opportunity: dict, evidence: dict, *, now: datetime | str | None = None) -> dict:
        """Preserve a sourced request for clarification, without qualifying work."""
        candidate = Opportunity.from_dict(opportunity)
        inquiry = qualify_inquiry(candidate, evidence, now)
        inquiry["route"] = assess_route(evidence, now)
        inquiry["proposal_ready"] = inquiry["inquiry_eligible"] and inquiry["route"]["proposal_ready"]
        with self.store.connection(write=True) as db:
            ledger = self._ledger(db)
            previous = ledger.state["opportunities"].get(candidate.id, {})
            assessment = ledger.register(candidate, set())
            assessment["qualified"], assessment["score"] = False, 0
            assessment["reasons"].append("qualification_inquiry_only")
            entry = ledger.state["opportunities"][candidate.id]
            for field in ("scope", "scope_hash", "evidence", "experiment_id"):
                if field in previous: entry[field] = previous[field]
            entry["inquiry_evidence"] = dict(evidence)
            entry["inquiry_assessment"] = inquiry
            entry["route_preflight"] = evidence.get("route_preflight")
            self._save(db, ledger)
            Store._event(db, "commercial_solicitation_assessed", opportunity_id=candidate.id, inquiry_eligible=inquiry["inquiry_eligible"], reasons=inquiry["reasons"])
            return inquiry

    def set_merchant_readiness(self, data: dict, receipt: str, *, checked_at: datetime | str | None = None) -> dict:
        if not isinstance(data, dict) or not isinstance(receipt, str) or not receipt:
            raise CommercialError("invalid_merchant", "Merchant readiness requires data and an operator verification receipt.")
        fields = data.get("customer_facing_identifiers", {})
        reasons = []
        if data.get("livemode") is not True: reasons.append("live_account_not_verified")
        for key in ("charges_enabled", "payouts_enabled", "details_submitted"):
            if data.get(key) is not True: reasons.append(key + "_not_enabled")
        if data.get("requirements_currently_due") != []: reasons.append("requirements_due_or_unknown")
        if not isinstance(fields, dict): fields = {}
        for key in ("business_name", "support_contact", "statement_descriptor"):
            if not isinstance(fields.get(key), str) or not fields[key].strip() or len(fields[key]) > 300: reasons.append(key + "_missing")
        record = {"ready": not reasons, "reasons": reasons, "receipt": receipt, "checked_at": _utc(checked_at).isoformat(), "customer_facing_identifiers": fields}
        with self.store.connection(write=True) as db:
            ledger = self._ledger(db)
            ledger.state["merchant"] = record
            self._save(db, ledger)
            Store._event(db, "commercial_merchant_verified", ready=record["ready"], reasons=reasons)
        return record

    @staticmethod
    def _merchant(ledger: CommercialLedger, current: datetime) -> dict:
        merchant = ledger.state.get("merchant", {})
        if not merchant.get("ready") or not 0 <= (current - _utc(merchant["checked_at"])).total_seconds() <= 86400:
            raise CommercialError("merchant_unavailable", "Live merchant and customer-facing identifiers need current verified readiness.")
        return merchant

    @staticmethod
    def _qualified(ledger: CommercialLedger, identifier: str, current: datetime, *, accepted_obligation: bool = False) -> dict:
        entry = ledger.state["opportunities"].get(identifier)
        if accepted_obligation and entry and ledger.state.get("agreements", {}).get(identifier, {}).get("scope_hash") == entry.get("scope_hash"):
            return entry  # A closed listing does not erase an accepted obligation.
        if not entry or not entry["assessment"]["qualified"]:
            raise CommercialError("unqualified_opportunity", "Action needs a qualified verified solicitation.")
        age = (current - _utc(entry["evidence"]["checked_at"])).total_seconds()
        if age < 0:
            raise CommercialError("source_evidence_future", "A future source receipt cannot justify a present commercial action.")
        if age > 86400:
            raise CommercialError("source_evidence_stale", "Refresh the solicitation before preparing or sending a new action.")
        if not assess_route(entry, current)["execution_ready"]:
            raise CommercialError("route_not_ready", "Verify participation costs, submission process and required enrollment before a new commercial action.")
        return entry

    @staticmethod
    def _inquiry(ledger: CommercialLedger, identifier: str, current: datetime) -> dict:
        entry = ledger.state["opportunities"].get(identifier)
        if not entry: raise CommercialError("unknown_opportunity", "Register a sourced solicitation before preparing an inquiry.")
        fields = {key: entry[key] for key in Opportunity.__dataclass_fields__}
        assessment = qualify_inquiry(fields, entry.get("inquiry_evidence", {}), current)
        if not assessment["inquiry_eligible"]:
            raise CommercialError("inquiry_not_eligible", "Inquiry needs a fresh low-risk public solicitation and verified contact permission: " + ", ".join(assessment["reasons"]))
        return entry

    @staticmethod
    def _prepare(db, key: str, kind: str, payload: dict) -> dict:
        if not isinstance(key, str) or not key or len(key) > 512: raise CommercialError("invalid_key", "Action needs a bounded idempotency key.")
        row = db.execute("SELECT * FROM outbox WHERE action_key=?", (key,)).fetchone()
        if row:
            if row["kind"] != kind or row["payload"] != encode(payload): raise ConflictError("Commercial action key reused for different content")
            return _action(row)
        identifier, now = uuid.uuid4().hex, time.time()
        db.execute("INSERT INTO outbox VALUES(?,?,NULL,?,?,'prepared',NULL,?,?)", (identifier, key, kind, encode(payload), now, now))
        Store._event(db, "commercial_action_prepared", action_id=identifier, kind=kind)
        return _action(db.execute("SELECT * FROM outbox WHERE id=?", (identifier,)).fetchone())

    @staticmethod
    def _agreement_customer(db, agreement: dict, opportunity_id: str) -> str:
        """Keep accepted billing identity independent of listing refreshes.

        Older records may recover this identity only from their original
        delivered quote and receipt, never from the mutable opportunity.
        Callers persist an inferred legacy binding in the same transaction.
        """
        customer = agreement.get("customer_reference")
        if isinstance(customer, str) and customer.strip():
            return customer
        row = db.execute("SELECT * FROM outbox WHERE id=?", (agreement.get("quote_action_id"),)).fetchone()
        if row and row["kind"] == "commerce_quote" and row["status"] == "delivered" and isinstance(row["external_ref"], str) and row["external_ref"].strip():
            quote = json.loads(row["payload"])
            customer = quote.get("customer_reference")
            if (quote.get("opportunity_id") == opportunity_id
                    and all(quote.get(key) == agreement.get(key) for key in ("scope_hash", "price_cents", "currency"))
                    and isinstance(customer, str) and customer.strip()):
                agreement["customer_reference"] = customer
                return customer
        raise CommercialError("agreement_target_unverified", "Accepted customer identity needs the original delivered quote and receipt before billing can proceed.")

    @classmethod
    def _invoice_agreement(cls, db, ledger: CommercialLedger, payload: dict) -> dict:
        agreement = ledger.state.get("agreements", {}).get(payload["opportunity_id"])
        if not agreement or payload.get("agreement_key") != agreement.get("quote_action_id") or any(payload.get(key) != agreement.get(key) for key in ("scope_hash", "price_cents", "currency")):
            raise CommercialError("agreement_required", "Invoice must retain the accepted customer's exact scope and price agreement.")
        if payload.get("customer_reference") != cls._agreement_customer(db, agreement, payload["opportunity_id"]):
            raise CommercialError("agreement_target_changed", "Invoice recipient must match the accepted customer's original quote.")
        return agreement

    def prepare_contact(self, opportunity_id: str, action_key: str, *, kind: str = "initial", experiment_id: str | None = None, now: datetime | str | None = None) -> dict:
        current = _utc(now)
        with self.store.connection(write=True) as db:
            ledger = self._ledger(db)
            entry = self._qualified(ledger, opportunity_id, current)
            ledger.reserve_contact(opportunity_id, kind=kind, idempotency_key=action_key, experiment_id=experiment_id, now=current)
            if experiment_id: entry["experiment_id"] = experiment_id
            deliverables = "; ".join(entry["scope"]["deliverables"])
            acceptance = "; ".join(entry["scope"]["acceptance"])
            prefix = "Following up once on" if kind == "followup" else "Responding to"
            message = f"{prefix} your request: {entry['title']}.\n\nRepvblicvs can provide: {deliverables}. Acceptance: {acceptance}.\n\nWe use AI-assisted production with reproducible checks and disclose that use. Your posted terms permit this approach. If the scope fits, we can agree on a fixed price and delivery before invoicing. Please confirm any remaining requirements."
            submission_route = _submission_route(entry)
            payload = {"opportunity_id": opportunity_id, "source_url": entry["source_url"], "customer_reference": entry["customer_reference"], "business_identity": "repvblicvs", "subject": entry["title"], "message": message, "contact_kind": kind, "experiment_id": experiment_id, "scope_hash": entry["scope_hash"],
                       "submission_route": submission_route, "submission_route_hash": _route_hash(submission_route)}
            action = self._prepare(db, action_key, "commerce_contact", payload)
            self._save(db, ledger)
            return action

    def prepare_inquiry(self, opportunity_id: str, action_key: str, *, questions: list[str] | None = None, kind: str = "initial", now: datetime | str | None = None) -> dict:
        """Reserve the SAME outreach allowance for a nonbinding qualification question."""
        selected = list(INQUIRY_QUESTIONS) if questions is None else questions
        if not isinstance(selected, list) or not selected or len(selected) > len(INQUIRY_QUESTIONS) or any(not isinstance(question, str) or question not in INQUIRY_QUESTIONS for question in selected) or len(set(selected)) != len(selected):
            raise CommercialError("invalid_inquiry", "Use distinct supported qualification question codes; source text cannot override the message.")
        current = _utc(now)
        with self.store.connection(write=True) as db:
            ledger = self._ledger(db)
            entry = self._inquiry(ledger, opportunity_id, current)
            ledger.reserve_contact(opportunity_id, kind=kind, idempotency_key=action_key, qualification_only=True, now=current)
            prefix = "Following up once regarding" if kind == "followup" else "Regarding"
            questions_text = "\n".join(f"- {INQUIRY_QUESTIONS[question]}" for question in selected)
            message = f"{prefix} your public contribution or work request at {entry['source_url']}.\n\nRepvblicvs is an AI-operated technical delivery business. Before considering a scope, could you clarify:\n\n{questions_text}\n\nThis is a nonbinding qualification inquiry. No assignment, delivery commitment, eligibility, price agreement or payment authorization is assumed. We will not claim independent human technical review or capabilities we have not verified."
            payload = {"opportunity_id": opportunity_id, "source_url": entry["source_url"], "customer_reference": entry["customer_reference"], "business_identity": "repvblicvs", "subject": "Current funded scope and AI workflow eligibility — Repvblicvs inquiry", "message": message, "contact_kind": kind, "qualification_only": True, "inquiry_purpose": "clarify_current_solicited_scope", "questions": selected,
                       "contact_permission_receipt": entry["inquiry_evidence"]["contact_permission_receipt"]}
            action = self._prepare(db, action_key, "commerce_inquiry", payload)
            self._save(db, ledger)
            return action

    def prepare_application(self, opportunity_id: str, action_key: str, *, subject: str, proposal: str, review_receipt: str, kind: str = "initial", now: datetime | str | None = None) -> dict:
        """Prepare a reviewed nonbinding response to a current public request.

        The trusted operator reviews the tailored proposal for truthful service
        fit and records that review; this does not certify human technical work.
        Source contents never choose policy, and preparation never sends, quotes,
        qualifies a job, accepts an assignment, or authorizes payment.
        """
        values = (("subject", subject, 200), ("proposal", proposal, 6000), ("review_receipt", review_receipt, 1000))
        for name, value, limit in values:
            if not isinstance(value, str) or not value.strip() or len(value) > limit or any((ord(char) < 32 and (name != "proposal" or char not in "\n\t")) or 127 <= ord(char) < 160 for char in value):
                raise CommercialError("invalid_application", "Application needs bounded reviewed text and a review receipt, without header or control-character injection.")
        current = _utc(now)
        with self.store.connection(write=True) as db:
            ledger = self._ledger(db)
            entry = self._inquiry(ledger, opportunity_id, current)
            route = assess_route(entry, current)
            if not route["proposal_ready"]:
                raise CommercialError("route_not_ready", "Application route needs verified access costs, submission steps and stage-specific payout prerequisites: " + ", ".join(route["reasons"]))
            ledger.reserve_contact(opportunity_id, kind=kind, idempotency_key=action_key, now=current, qualification_only=True)
            message = ("Repvblicvs is an AI-operated technical delivery business responding to your public solicitation at "
                       + entry["source_url"] + ".\n\n" + proposal.strip()
                       + "\n\nThis is a nonbinding application for qualification only. No assignment, delivery commitment, eligibility, price agreement or payment authorization is assumed. Please confirm supplier eligibility and mutually agreed scope before work begins. We do not claim independent human technical review or unverified capabilities.")
            submission_route = _submission_route(entry)
            payload = {"opportunity_id": opportunity_id, "source_url": entry["source_url"], "customer_reference": entry["customer_reference"], "business_identity": "repvblicvs", "subject": subject.strip(), "message": message, "contact_kind": kind, "qualification_only": True, "application_purpose": "nonbinding_solicited_application", "proposal_review_receipt": review_receipt.strip(),
                       "submission_route": submission_route, "submission_route_hash": _route_hash(submission_route)}
            action = self._prepare(db, action_key, "commerce_application", payload)
            self._save(db, ledger)
            return action

    def prepare_quote(self, opportunity_id: str, price_cents: int, action_key: str, *, currency: str = "USD", now: datetime | str | None = None) -> dict:
        _amount(price_cents, currency)
        current = _utc(now)
        with self.store.connection(write=True) as db:
            ledger = self._ledger(db)
            entry = self._qualified(ledger, opportunity_id, current)
            merchant = self._merchant(ledger, current)
            if not any(item["opportunity_id"] == opportunity_id and item["event"] == "reply" for item in ledger.state["events"]):
                raise CommercialError("conversation_not_established", "A customer reply or verified inbound request is required before a quote.")
            agreement = ledger.state.get("agreements", {}).get(opportunity_id)
            if agreement and entry["customer_reference"] != self._agreement_customer(db, agreement, opportunity_id):
                raise CommercialError("source_target_changed", "Review the accepted customer's conversation before quoting a changed listing target.")
            payload = {"opportunity_id": opportunity_id, "business_identity": "repvblicvs", "customer_reference": entry["customer_reference"], "scope": entry["scope"], "scope_hash": entry["scope_hash"], "price_cents": price_cents, "currency": currency, "customer_facing_identifiers": merchant["customer_facing_identifiers"], "ai_disclosure": "AI-assisted production with validated deliverables", "message": f"Fixed scope quote: {currency} {price_cents / 100:.2f}. Deliverables: {'; '.join(entry['scope']['deliverables'])}. Acceptance: {'; '.join(entry['scope']['acceptance'])}. AI-assisted production with validated deliverables. Confirm this scope and price before invoicing."}
            action = self._prepare(db, action_key, "commerce_quote", payload)
            self._save(db, ledger)
            return action

    def agree_scope(self, opportunity_id: str, quote_action_id: str, customer_receipt: str, *, now: datetime | str | None = None) -> dict:
        if not isinstance(customer_receipt, str) or not customer_receipt: raise CommercialError("agreement_receipt_required", "Scope and price agreement require a customer receipt.")
        with self.store.connection(write=True) as db:
            ledger = self._ledger(db)
            row = _action(db.execute("SELECT * FROM outbox WHERE id=?", (quote_action_id,)).fetchone())
            if row["kind"] != "commerce_quote" or row["status"] != "delivered" or row["payload"]["opportunity_id"] != opportunity_id:
                raise CommercialError("invalid_agreement", "Agreement must match the delivered quote for this customer.")
            payload = row["payload"]
            if not isinstance(payload.get("customer_reference"), str) or not payload["customer_reference"].strip():
                raise CommercialError("agreement_target_unverified", "The delivered quote must identify the accepted customer before agreement can be recorded.")
            agreement = {"quote_action_id": quote_action_id, "scope_hash": payload["scope_hash"], "price_cents": payload["price_cents"], "currency": payload["currency"], "customer_reference": payload["customer_reference"], "customer_receipt": customer_receipt, "at": _utc(now).isoformat()}
            previous = ledger.state.setdefault("agreements", {}).get(opportunity_id)
            if previous:
                self._agreement_customer(db, previous, opportunity_id)
            if previous and any(previous[key] != agreement[key] for key in ("quote_action_id", "scope_hash", "price_cents", "currency", "customer_reference", "customer_receipt")):
                raise CommercialError("agreement_conflict", "A different customer agreement is already recorded.")
            ledger.state["agreements"][opportunity_id] = previous or agreement
            ledger.record_event(opportunity_id, "agreed", amount_cents=payload["price_cents"], idempotency_key="agreement:" + quote_action_id, experiment_id=ledger.state["opportunities"][opportunity_id].get("experiment_id"), now=now)
            self._save(db, ledger)
            return previous or agreement

    def prepare_invoice(self, opportunity_id: str, action_key: str, *, now: datetime | str | None = None) -> dict:
        current = _utc(now)
        with self.store.connection(write=True) as db:
            ledger = self._ledger(db)
            entry = self._qualified(ledger, opportunity_id, current, accepted_obligation=True)
            merchant = self._merchant(ledger, current)
            agreement = ledger.state.get("agreements", {}).get(opportunity_id)
            if not agreement or agreement["scope_hash"] != entry["scope_hash"]:
                raise CommercialError("agreement_required", "Customer must agree to the current scope and price before invoicing.")
            customer = self._agreement_customer(db, agreement, opportunity_id)
            previous = next((row for row in db.execute("SELECT * FROM outbox WHERE kind='commerce_invoice'") if json.loads(row["payload"])["agreement_key"] == agreement["quote_action_id"]), None)
            if previous:
                if previous["action_key"] != action_key: raise CommercialError("duplicate_invoice", "An invoice already exists for this agreement; reconcile it instead.")
                self._invoice_agreement(db, ledger, json.loads(previous["payload"]))
                self._save(db, ledger)
                return _action(previous)
            payload = {"opportunity_id": opportunity_id, "business_identity": "repvblicvs", "customer_reference": customer, "scope": entry["scope"], "scope_hash": entry["scope_hash"], "price_cents": agreement["price_cents"], "currency": agreement["currency"], "agreement_key": agreement["quote_action_id"], "customer_agreement_receipt": agreement["customer_receipt"], "customer_facing_identifiers": merchant["customer_facing_identifiers"], "charge_automatically": False}
            self._save(db, ledger)
            return self._prepare(db, action_key, "commerce_invoice", payload)

    def record_event(self, opportunity_id: str, event: str, *, idempotency_key: str, amount_cents: int = 0, currency: str = "USD", experiment_id: str | None = None, now: datetime | str | None = None, receipt: str) -> dict:
        if not isinstance(receipt, str) or not receipt: raise CommercialError("receipt_required", "Business events require a source or financial receipt.")
        with self.store.connection(write=True) as db:
            ledger = self._ledger(db)
            if experiment_id is None: experiment_id = ledger.state["opportunities"].get(opportunity_id, {}).get("experiment_id")
            item = ledger.record_event(opportunity_id, event, amount_cents=amount_cents, currency=currency, idempotency_key=idempotency_key, experiment_id=experiment_id, now=now)
            if item.get("receipt") and item["receipt"] != receipt: raise CommercialError("receipt_conflict", "Event receipt cannot be replaced.")
            item["receipt"] = receipt
            self._save(db, ledger)
            return item

    def review_experiments(self, now: datetime | str | None = None) -> list[dict]:
        with self.store.connection(write=True) as db:
            ledger = self._ledger(db)
            windows = {identifier: json.loads(encode({key: item[key] for key in ("offer", "capability", "price_band_cents", "created_at", "reviewed_contacts") if key in item})) for identifier, item in ledger.state["experiments"].items() if item["status"] == "active"}
            result = ledger.review_experiments(now)
            for decision in result:
                if decision["due"]:
                    experiment = ledger.state["experiments"][decision["experiment_id"]]
                    experiment.setdefault("review_history", []).append({**json.loads(encode(experiment["last_review"])), "evaluated_offer": windows[decision["experiment_id"]]})
            self._save(db, ledger)
            for decision in result:
                if decision["due"]: Store._event(db, "commercial_experiment_reviewed", **decision)
            return result

    def _resolved(self, action_id: str, result: dict, current: datetime, *, reconciliation: bool = False) -> dict:
        with self.store.connection(write=True) as db:
            action = _action(db.execute("SELECT * FROM outbox WHERE id=?", (action_id,)).fetchone())
            ledger = self._ledger(db)
            if action["status"] == "delivered": return action
            status, external_ref = result.get("status"), result.get("external_ref")
            confirmed = status == "confirmed" and isinstance(external_ref, str) and bool(external_ref)
            absent = reconciliation and status == "not_sent" and isinstance(external_ref, str) and bool(external_ref)
            new_status = "delivered" if confirmed else "prepared" if absent else "unknown"
            if action["kind"] in {"commerce_contact", "commerce_inquiry", "commerce_application"}:
                if confirmed: ledger.resolve_contact(action["action_key"], "confirmed", receipt=external_ref, now=current)
                elif absent:
                    item = next(item for item in ledger.state["outreach"] if item["idempotency_key"] == action["action_key"])
                    item["state"], item["reconciliation_receipt"] = "reserved", external_ref
                else: ledger.resolve_contact(action["action_key"], "uncertain", now=current)
            if confirmed and action["kind"] == "commerce_quote":
                identifier = action["payload"]["opportunity_id"]
                ledger.record_event(identifier, "quoted", amount_cents=action["payload"]["price_cents"], idempotency_key="quote:" + action["id"], experiment_id=ledger.state["opportunities"][identifier].get("experiment_id"), now=current)
            db.execute("UPDATE outbox SET status=?,external_ref=?,updated=? WHERE id=?", (new_status, external_ref if isinstance(external_ref, str) else None, time.time(), action_id))
            self._save(db, ledger)
            Store._event(db, "commercial_action_resolved", action_id=action_id, status=new_status, reconciliation=reconciliation)
            return _action(db.execute("SELECT * FROM outbox WHERE id=?", (action_id,)).fetchone())

    def dispatch(self, action_id: str, transport: Transport, *, now: datetime | str | None = None) -> dict:
        current = _utc(now)
        with self.store.connection(write=True) as db:
            action = _action(db.execute("SELECT * FROM outbox WHERE id=?", (action_id,)).fetchone())
            if action["kind"] not in {"commerce_contact", "commerce_inquiry", "commerce_application", "commerce_quote", "commerce_invoice"}: raise CommercialError("unsupported_action", "Only typed commercial intentions can use this transport.")
            if action["status"] != "prepared": return action  # Includes ambiguous/crashed calls.
            ledger = self._ledger(db)
            if action["kind"] in {"commerce_inquiry", "commerce_application"}:
                entry = self._inquiry(ledger, action["payload"]["opportunity_id"], current)
                if action["kind"] == "commerce_application" and not assess_route(entry, current)["proposal_ready"]:
                    raise CommercialError("route_not_ready", "Refresh application access costs, submission process and required enrollment before dispatch.")
                if action["kind"] == "commerce_inquiry" and action["payload"].get("contact_permission_receipt") != entry["inquiry_evidence"]["contact_permission_receipt"]:
                    raise CommercialError("contact_permission_changed", "Review a new inquiry when its verified contact permission changes.")
            else: entry = self._qualified(ledger, action["payload"]["opportunity_id"], current, accepted_obligation=action["kind"] == "commerce_invoice")
            if action["kind"] in {"commerce_contact", "commerce_inquiry", "commerce_application"} and any(entry[field] != action["payload"][field] for field in ("source_url", "customer_reference")):
                raise CommercialError("source_target_changed", "Refresh the reviewed contact when its verified target changes.")
            if action["kind"] == "commerce_quote":
                if entry["customer_reference"] != action["payload"]["customer_reference"]:
                    raise CommercialError("source_target_changed", "Review a new quote when its verified customer target changes.")
                agreement = ledger.state.get("agreements", {}).get(action["payload"]["opportunity_id"])
                if agreement and action["payload"]["customer_reference"] != self._agreement_customer(db, agreement, action["payload"]["opportunity_id"]):
                    raise CommercialError("agreement_target_changed", "Quote recipient must remain the accepted customer.")
            if action["kind"] == "commerce_invoice":
                self._invoice_agreement(db, ledger, action["payload"])
            if action["kind"] in {"commerce_contact", "commerce_application"}:
                route_snapshot = _submission_route(entry)
                if action["payload"].get("submission_route") != route_snapshot or action["payload"].get("submission_route_hash") != _route_hash(route_snapshot):
                    raise CommercialError("submission_route_changed", "Review a new contact or application when its submission route, costs, or payout prerequisites change.")
            if action["kind"] in {"commerce_quote", "commerce_invoice"}: self._merchant(ledger, current)
            if action["kind"] != "commerce_invoice" and any(item["opportunity_id"] == action["payload"]["opportunity_id"] and item["event"] in {"declined", "unsubscribed"} for item in ledger.state["events"]):
                raise CommercialError("contact_suppressed", "Customer declined or unsubscribed after preparation.")
            if action["kind"] in {"commerce_contact", "commerce_inquiry", "commerce_application"}:
                item = next(item for item in ledger.state["outreach"] if item["idempotency_key"] == action["action_key"])
                daily_limit = contact_policy(ledger.state.get("contact_policy"))["daily_contact_limit"]
                if item["kind"] == "followup" and any(event["opportunity_id"] == item["opportunity_id"] and event["event"] == "reply" for event in ledger.state["events"]):
                    raise CommercialError("conversation_active", "A new customer reply supersedes the prepared automatic follow-up.")
                today = current.astimezone(ledger.timezone).date()
                if _utc(item["reserved_at"]).astimezone(ledger.timezone).date() != today:
                    count = sum(other["kind"] == item["kind"] and other["state"] != "cancelled" and _utc(other["reserved_at"]).astimezone(ledger.timezone).date() == today for other in ledger.state["outreach"])
                    if daily_limit is not None and count >= daily_limit: raise CommercialError("daily_contact_cap", "Deferred contact exceeds the private configured limit on its actual send day.")
                    item["reserved_at"] = current.isoformat()
            self._save(db, ledger)
            db.execute("UPDATE outbox SET status='dispatching',updated=? WHERE id=?", (time.time(), action_id))
            Store._event(db, "commercial_dispatch_started", action_id=action_id)
        # Commit before the connector call: a crash is ambiguous, never replayed.
        try:
            callback = transport.invoice if action["kind"] == "commerce_invoice" else transport.send
            result = callback(action["payload"], idempotency_key=action["action_key"])
            if not isinstance(result, dict): result = {"status": "unknown"}
        except Exception:
            result = {"status": "unknown"}
        return self._resolved(action_id, result, current)

    def dispatch_batch(self, action_ids: list[str], transport: Transport, *, now: datetime | str | None = None) -> list[dict]:
        """Dispatch a finite reviewed list, preserving per-action recovery gates.

        This is a per-invocation bound rather than a daily throughput restriction.
        An oversized, duplicate or invalid batch fails before any callback.
        """
        current = _utc(now)
        with self.store.connection() as db:
            ledger = self._ledger(db)
            limit = contact_policy(ledger.state.get("contact_policy"))["max_dispatch_batch"]
            if not isinstance(action_ids, list) or not 1 <= len(action_ids) <= limit or any(not isinstance(identifier, str) or not identifier or len(identifier) > 128 for identifier in action_ids) or len(set(action_ids)) != len(action_ids):
                raise CommercialError("invalid_contact_batch", "Supply distinct reviewed contact action identifiers within the private invocation limit.")
            for identifier in action_ids:
                action = _action(db.execute("SELECT * FROM outbox WHERE id=?", (identifier,)).fetchone())
                if action["kind"] not in {"commerce_contact", "commerce_inquiry", "commerce_application"}:
                    raise CommercialError("invalid_contact_batch", "A contact batch cannot dispatch quotes, invoices or other action kinds.")
        return [self.dispatch(identifier, transport, now=current) for identifier in action_ids]

    def reconcile(self, action_id: str, transport: Transport, *, now: datetime | str | None = None) -> dict:
        with self.store.connection() as db:
            action = _action(db.execute("SELECT * FROM outbox WHERE id=?", (action_id,)).fetchone())
        if action["status"] not in {"unknown", "dispatching"}: return action
        if action["status"] == "dispatching" and time.time() - action["updated"] < 120:
            return action  # Do not race a live connector callback with a receipt query.
        try: result = transport.receipt(action)
        except Exception: result = {"status": "unknown"}
        if not isinstance(result, dict): result = {"status": "unknown"}
        return self._resolved(action_id, result, _utc(now), reconciliation=True)

    def reserve_execution(self, task_id: str, classification: str, units: float) -> dict:
        """Reserve measured execution seconds; exploratory capacity is ≤10%."""
        if classification not in PRIORITIES or isinstance(units, bool) or not isinstance(units, (int, float)) or not math.isfinite(units) or units <= 0:
            raise CommercialError("invalid_execution", "Execution needs a known class and positive bounded measured units.")
        with self.store.connection(write=True) as db:
            previous = db.execute("SELECT * FROM commerce_execution WHERE task_id=?", (task_id,)).fetchone()
            if previous:
                if previous["classification"] != classification or previous["reserved_units"] != units: raise ConflictError("Execution reservation identifier reused")
                return dict(previous)
            if classification == "exploratory_research":
                if db.execute("SELECT 1 FROM tasks WHERE status IN ('queued','running') AND priority>=80 LIMIT 1").fetchone():
                    raise CommercialError("customer_work_pending", "Customer delivery and acquisition precede independent exploratory research.")
                rows = db.execute("SELECT * FROM commerce_execution").fetchall()
                productive = sum(row["actual_units"] or 0 for row in rows if row["classification"] != classification and row["status"] == "completed")
                research = sum(row["actual_units"] if row["actual_units"] is not None else row["reserved_units"] for row in rows if row["classification"] == classification)
                if research + units > productive / 9 + 1e-9:
                    raise CommercialError("research_capacity", "Independent exploratory R&D is limited to ten percent of executed capacity.")
            db.execute("INSERT INTO commerce_execution VALUES(?,?,?,NULL,'reserved',?)", (task_id, classification, units, time.time()))
            return dict(db.execute("SELECT * FROM commerce_execution WHERE task_id=?", (task_id,)).fetchone())

    def complete_execution(self, task_id: str, actual_units: float) -> dict:
        if isinstance(actual_units, bool) or not isinstance(actual_units, (int, float)) or not math.isfinite(actual_units) or actual_units < 0:
            raise CommercialError("invalid_execution", "Actual execution units must be finite and nonnegative.")
        with self.store.connection(write=True) as db:
            row = db.execute("SELECT * FROM commerce_execution WHERE task_id=?", (task_id,)).fetchone()
            if not row: raise CommercialError("unknown_execution", "Execution reservation is missing.")
            if row["status"] != "reserved":
                if row["actual_units"] != actual_units: raise ConflictError("Actual execution cannot be overwritten")
                return dict(row)
            status = "overrun" if row["classification"] == "exploratory_research" and actual_units > row["reserved_units"] else "completed"
            db.execute("UPDATE commerce_execution SET actual_units=?,status=? WHERE task_id=?", (actual_units, status, task_id))
            Store._event(db, "commercial_execution_recorded", task_id, classification=row["classification"], actual_units=actual_units, status=status)
            return dict(db.execute("SELECT * FROM commerce_execution WHERE task_id=?", (task_id,)).fetchone())
