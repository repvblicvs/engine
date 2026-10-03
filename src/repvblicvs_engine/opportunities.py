"""Qualification, bounded outreach, offer experiments, and cash accounting.

The ledger is a JSON-serializable object owned by the caller. Persist it inside
the engine's serialized transaction boundary. This module never sends a message,
accepts a contract, spends money, or assumes a discovered lead permits AI use.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


class OpportunityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(message)


CATEGORIES = {"software", "data", "web", "research", "documents", "creative"}
FINANCIAL_EVENTS = {"quoted", "agreed", "paid", "refunded", "fee", "compute_cost", "settled", "funds_available"}
EVENTS = FINANCIAL_EVENTS | {"qualified", "reply", "delivered", "declined", "unsubscribed"}


def _utc(value: datetime | str | None = None) -> datetime:
    if value is None: return datetime.now(timezone.utc)
    if isinstance(value, str):
        try: value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc: raise OpportunityError("invalid_time", "Use an ISO 8601 timestamp.") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OpportunityError("invalid_time", "Times must include a timezone.")
    return value.astimezone(timezone.utc)


def valid_source_url(value: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(character.isspace() or ord(character) < 32 for character in value): return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
        if parsed.scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password or parsed.fragment: return False
        if port not in {None, 80, 443} or hostname.endswith(".") or hostname.lower() in {"localhost", "localhost.localdomain"} or hostname.lower().endswith((".local", ".internal", ".localhost")): return False
        try:
            return ipaddress.ip_address(hostname).is_global
        except ValueError:
            labels = hostname.split(".")
            return len(labels) >= 2 and all(label and len(label) <= 63 and label[0] != "-" and label[-1] != "-" and all(c.isascii() and (c.isalnum() or c == "-") for c in label) for label in labels)
    except ValueError:
        return False


@dataclass(frozen=True)
class Opportunity:
    source_url: str
    title: str
    source_quote: str
    category: str
    ai_permission: str = "unknown"
    solicited: bool = False
    risk: str = "unknown"
    requires_attestation: bool = False
    requires_credential: bool = False
    requires_new_spending: bool = False
    capability: str = ""
    customer_reference: str = ""

    @property
    def id(self) -> str:
        return hashlib.sha256((self.source_url + "\n" + self.title).encode()).hexdigest()[:20]

    @classmethod
    def from_dict(cls, payload: dict) -> Opportunity:
        try: opportunity = cls(**payload)
        except (TypeError, ValueError) as exc: raise OpportunityError("invalid_opportunity", "Opportunity fields are invalid.") from exc
        opportunity.validate()
        return opportunity

    def validate(self) -> None:
        if not valid_source_url(self.source_url): raise OpportunityError("invalid_source", "Opportunity requires a public HTTP(S) source URL without credentials.")
        if not isinstance(self.title, str) or not self.title.strip() or len(self.title) > 300 or not isinstance(self.source_quote, str) or not self.source_quote.strip() or len(self.source_quote) > 2000:
            raise OpportunityError("invalid_evidence", "Provide a bounded title and a source excerpt establishing the request.")
        if self.category not in CATEGORIES or self.ai_permission not in {"allowed", "unknown", "prohibited"} or self.risk not in {"low", "medium", "high", "unknown"}:
            raise OpportunityError("invalid_classification", "Opportunity category, AI permission, or risk is invalid.")
        if any(not isinstance(value, bool) for value in (self.solicited, self.requires_attestation, self.requires_credential, self.requires_new_spending)):
            raise OpportunityError("invalid_classification", "Eligibility flags must be booleans.")
        if not isinstance(self.capability, str) or not isinstance(self.customer_reference, str):
            raise OpportunityError("invalid_classification", "Capability and customer references must be strings.")


def qualify_opportunity(opportunity: Opportunity | dict, available_capabilities: set[str] | None = None) -> dict:
    opportunity = Opportunity.from_dict(opportunity) if isinstance(opportunity, dict) else opportunity
    opportunity.validate()
    reasons = []
    if not opportunity.solicited: reasons.append("solicitation_not_established")
    if opportunity.ai_permission != "allowed": reasons.append("ai_permission_not_established" if opportunity.ai_permission == "unknown" else "ai_prohibited")
    if opportunity.risk != "low": reasons.append("risk_exceeds_initial_charter")
    if opportunity.requires_attestation: reasons.append("personal_attestation_required")
    if opportunity.requires_credential: reasons.append("professional_credential_required")
    if opportunity.requires_new_spending: reasons.append("new_spending_required")
    if not opportunity.capability: reasons.append("delivery_capability_unspecified")
    elif available_capabilities is not None and opportunity.capability not in available_capabilities: reasons.append("delivery_capability_unavailable")
    return {"opportunity_id": opportunity.id, "qualified": not reasons, "reasons": reasons, "score": 100 if not reasons else 0, "category": opportunity.category}


def qualify_inquiry(opportunity: Opportunity | dict, evidence: dict, now: datetime | str | None = None) -> dict:
    """Allow a nonbinding qualification contact for a public solicitation only.

    Unknown AI eligibility and delivery capability are questions, never claims.
    Evidence is a trusted operator receipt; source text cannot supply policy.
    """
    opportunity = Opportunity.from_dict(opportunity) if isinstance(opportunity, dict) else opportunity
    opportunity.validate()
    if not isinstance(evidence, dict): raise OpportunityError("invalid_evidence", "Inquiry evidence must be a verified source record.")
    reasons = []
    if not opportunity.solicited: reasons.append("solicitation_not_established")
    if opportunity.ai_permission == "prohibited": reasons.append("ai_prohibited")
    if opportunity.risk != "low": reasons.append("risk_exceeds_initial_charter")
    if opportunity.requires_attestation or opportunity.requires_credential or opportunity.requires_new_spending:
        reasons.append("nondelegable_or_spending_requirement")
    if not opportunity.customer_reference.strip(): reasons.append("contact_target_missing")
    for key in ("source_read_receipt", "solicitation_receipt", "contact_permission_receipt"):
        if not isinstance(evidence.get(key), str) or not evidence[key].strip(): reasons.append(key + "_missing")
    if evidence.get("currently_open") is not True: reasons.append("request_not_verified_open")
    if evidence.get("archived") is not False or evidence.get("repository_archived") is True: reasons.append("source_archived_or_unknown")
    if evidence.get("public_contact_permitted") is not True: reasons.append("public_contact_not_permitted")
    checked = evidence.get("checked_at")
    if not checked: reasons.append("freshness_missing")
    else:
        age = (_utc(now) - _utc(checked)).total_seconds()
        if age < 0: reasons.append("source_evidence_future")
        elif age > 86400: reasons.append("source_evidence_stale")
    return {"opportunity_id": opportunity.id, "inquiry_eligible": not reasons, "qualified": False, "reasons": reasons, "qualification_only": True}


def _business_days_after(start: datetime, count: int, tz: ZoneInfo) -> datetime:
    value, added = start.astimezone(tz), 0
    while added < count:
        value += timedelta(days=1)
        if value.weekday() < 5: added += 1
    return value.astimezone(timezone.utc)


def review_experiment(experiment: dict, now: datetime | str | None = None) -> dict:
    current = _utc(now)
    created = _utc(experiment["created_at"])
    count = experiment.get("qualified_contacts", 0)
    due = count >= 10 or current - created >= timedelta(days=7)
    if not due: return {"due": False, "decision": "continue", "reason": "evaluation_window_open"}
    if experiment.get("cleared_net_cents", 0) > 0:
        return {"due": True, "decision": "continue", "reason": "positive_cleared_contribution"}
    if experiment.get("replies", 0) > 0 or experiment.get("deliveries", 0) > 0:
        return {"due": True, "decision": "adjust", "reason": "interest_without_positive_cleared_contribution"}
    return {"due": True, "decision": "replace", "reason": "no_observed_conversion"}


class CommercialLedger:
    """Policy state; callers must save and serialize updates atomically."""

    def __init__(self, state: dict | None = None, timezone_name: str = "America/New_York") -> None:
        self.state = state if state is not None else {}
        self.timezone = ZoneInfo(timezone_name)
        for key, default in (("opportunities", {}), ("events", []), ("outreach", []), ("experiments", {})):
            self.state.setdefault(key, default)

    def register(self, opportunity: Opportunity | dict, available_capabilities: set[str] | None = None) -> dict:
        opportunity = Opportunity.from_dict(opportunity) if isinstance(opportunity, dict) else opportunity
        assessment = qualify_opportunity(opportunity, available_capabilities)
        self.state["opportunities"][opportunity.id] = {**asdict(opportunity), "assessment": assessment}
        return assessment

    def record_event(self, opportunity_id: str, event: str, *, amount_cents: int = 0, currency: str = "USD", idempotency_key: str, now: datetime | str | None = None, experiment_id: str | None = None) -> dict:
        if opportunity_id not in self.state["opportunities"]: raise OpportunityError("unknown_opportunity", "Register an opportunity before recording business events.")
        if event not in EVENTS: raise OpportunityError("invalid_event", "Unsupported business event.")
        if currency != "USD" or isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents < 0:
            raise OpportunityError("invalid_amount", "Initial ledger amounts must be nonnegative integer USD cents.")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip(): raise OpportunityError("invalid_key", "Events need a nonempty idempotency key.")
        proposed = {"opportunity_id": opportunity_id, "event": event, "amount_cents": amount_cents, "currency": currency, "idempotency_key": idempotency_key, "experiment_id": experiment_id}
        existing = next((item for item in self.state["events"] if item["idempotency_key"] == idempotency_key), None)
        if existing:
            if any(existing.get(key) != value for key, value in proposed.items()): raise OpportunityError("idempotency_conflict", "Idempotency key already belongs to a different event.")
            return existing
        if experiment_id is not None and experiment_id not in self.state["experiments"]: raise OpportunityError("unknown_experiment", "Experiment is not registered.")
        item = {**proposed, "at": _utc(now).isoformat()}
        self.state["events"].append(item)
        return item

    def financial_summary(self, experiment_id: str | None = None) -> dict:
        events = [item for item in self.state["events"] if experiment_id is None or item["experiment_id"] == experiment_id]
        totals = {kind: sum(item["amount_cents"] for item in events if item["event"] == kind) for kind in FINANCIAL_EVENTS}
        cleared_net = totals["settled"] - totals["refunded"] - totals["fee"] - totals["compute_cost"]
        return {"currency": "USD", "amounts_cents": totals, "cleared_net_cents": cleared_net, "usable_funds_cents": max(0, min(totals["funds_available"], cleared_net)), "counts": {kind: sum(item["event"] == kind for item in events) for kind in EVENTS}, "spending_authorized": False}

    def activate_experiment(self, experiment_id: str, offer: str, now: datetime | str | None = None) -> dict:
        if not isinstance(experiment_id, str) or not experiment_id or not isinstance(offer, str) or not offer.strip(): raise OpportunityError("invalid_experiment", "Experiments require an identifier and offer.")
        if experiment_id in self.state["experiments"]: return self.state["experiments"][experiment_id]
        if sum(item["status"] == "active" for item in self.state["experiments"].values()) >= 3:
            raise OpportunityError("experiment_capacity", "At most three offer experiments may run concurrently.")
        item = {"id": experiment_id, "offer": offer, "created_at": _utc(now).isoformat(), "status": "active"}
        self.state["experiments"][experiment_id] = item
        return item

    def review_experiments(self, now: datetime | str | None = None) -> list[dict]:
        decisions = []
        for experiment in self.state["experiments"].values():
            if experiment["status"] != "active": continue
            identifier = experiment["id"]
            events = [item for item in self.state["events"] if item["experiment_id"] == identifier]
            contacts = sum(item["kind"] == "initial" and item["state"] == "confirmed" and not item.get("qualification_only", False) and item.get("experiment_id") == identifier for item in self.state["outreach"])
            decision = review_experiment({**experiment, "qualified_contacts": contacts - experiment.get("reviewed_contacts", 0), "replies": sum(item["event"] == "reply" for item in events), "deliveries": sum(item["event"] == "delivered" for item in events), "cleared_net_cents": self.financial_summary(identifier)["cleared_net_cents"]}, now)
            if decision["due"]:
                experiment["last_review"] = {**decision, "at": _utc(now).isoformat()}
                if decision["decision"] == "replace": experiment["status"] = "retired"
                else:
                    experiment["created_at"] = _utc(now).isoformat()
                    experiment["reviewed_contacts"] = contacts
            decisions.append({"experiment_id": identifier, **decision})
        return decisions

    def reserve_contact(self, opportunity_id: str, *, kind: str = "initial", idempotency_key: str, now: datetime | str | None = None, experiment_id: str | None = None, qualification_only: bool = False) -> dict:
        current = _utc(now)
        opportunity = self.state["opportunities"].get(opportunity_id)
        if not isinstance(qualification_only, bool): raise OpportunityError("invalid_contact", "Qualification-only contact flag must be a boolean.")
        if qualification_only:
            if not opportunity: raise OpportunityError("unknown_opportunity", "Register a verified solicitation before asking qualification questions.")
            fields = {key: opportunity[key] for key in Opportunity.__dataclass_fields__}
            eligibility = qualify_inquiry(fields, opportunity.get("inquiry_evidence", {}), current)
            if not eligibility["inquiry_eligible"]: raise OpportunityError("inquiry_not_eligible", "Inquiry requires a current low-risk solicitation and verified public contact permission.")
        elif not opportunity or not opportunity["assessment"]["qualified"]: raise OpportunityError("unqualified_opportunity", "Contact requires a qualified, sourced solicitation with permitted AI use.")
        if kind not in {"initial", "followup"} or not isinstance(idempotency_key, str) or not idempotency_key:
            raise OpportunityError("invalid_contact", "Contact requires a supported kind and idempotency key.")
        existing = next((item for item in self.state["outreach"] if item["idempotency_key"] == idempotency_key), None)
        if existing:
            if existing["opportunity_id"] != opportunity_id or existing["kind"] != kind or existing.get("experiment_id") != experiment_id or existing.get("qualification_only", False) != qualification_only:
                raise OpportunityError("idempotency_conflict", "Contact key already belongs to another action.")
            return existing
        if experiment_id is not None and (experiment_id not in self.state["experiments"] or self.state["experiments"][experiment_id]["status"] != "active"):
            raise OpportunityError("unknown_experiment", "Contact experiment must be active.")
        if any(item["opportunity_id"] == opportunity_id and item["event"] in {"declined", "unsubscribed"} for item in self.state["events"]):
            raise OpportunityError("contact_suppressed", "Customer declined or unsubscribed.")
        active = [item for item in self.state["outreach"] if item["state"] != "cancelled"]
        if any(item["opportunity_id"] == opportunity_id and item["kind"] == kind for item in active):
            raise OpportunityError("duplicate_contact", "This contact was already reserved or attempted; reconcile its receipt.")
        customer = opportunity.get("customer_reference")
        if kind == "initial" and customer and any(item["kind"] == "initial" and self.state["opportunities"][item["opportunity_id"]].get("customer_reference") == customer for item in active):
            raise OpportunityError("duplicate_customer_contact", "This customer already has an initial contact; handle their requests in the existing conversation.")
        day = current.astimezone(self.timezone).date()
        if sum(item["kind"] == kind and _utc(item["reserved_at"]).astimezone(self.timezone).date() == day for item in active) >= 3:
            raise OpportunityError("daily_contact_cap", "At most three initial contacts and three eligible follow-ups may be reserved per day.")
        if kind == "followup":
            initial = next((item for item in active if item["opportunity_id"] == opportunity_id and item["kind"] == "initial" and item["state"] == "confirmed"), None)
            if not initial: raise OpportunityError("followup_without_initial", "A confirmed initial contact is required.")
            if current < _business_days_after(_utc(initial["confirmed_at"]), 3, self.timezone):
                raise OpportunityError("followup_too_early", "Wait three business days after confirmed initial contact.")
            if any(item["opportunity_id"] == opportunity_id and item["event"] == "reply" for item in self.state["events"]):
                raise OpportunityError("conversation_active", "A reply should be handled as a conversation, not an automatic follow-up.")
        item = {"opportunity_id": opportunity_id, "kind": kind, "idempotency_key": idempotency_key, "reserved_at": current.isoformat(), "state": "reserved", "experiment_id": experiment_id, "qualification_only": qualification_only}
        self.state["outreach"].append(item)
        return item

    def resolve_contact(self, idempotency_key: str, state: str, *, receipt: str = "", now: datetime | str | None = None) -> dict:
        if state not in {"confirmed", "uncertain", "cancelled"}: raise OpportunityError("invalid_contact_state", "Resolve contact as confirmed, uncertain, or cancelled.")
        item = next((item for item in self.state["outreach"] if item["idempotency_key"] == idempotency_key), None)
        if not item: raise OpportunityError("unknown_contact", "Contact reservation is missing.")
        if item["state"] == "confirmed" and state != "confirmed": raise OpportunityError("contact_already_confirmed", "A confirmed external action cannot be cancelled.")
        if item["state"] == "uncertain" and state == "cancelled" and not receipt:
            raise OpportunityError("reconciliation_required", "An uncertain external action needs a no-delivery reconciliation receipt before cancellation.")
        if item["state"] == "confirmed" and receipt != item.get("receipt"):
            raise OpportunityError("receipt_conflict", "A confirmed contact receipt cannot be replaced.")
        if state == "confirmed" and not receipt: raise OpportunityError("receipt_required", "Confirmation requires an external receipt identifier.")
        item["state"], item["receipt"] = state, receipt
        if state == "confirmed": item.setdefault("confirmed_at", _utc(now).isoformat())
        return item

    def export_state(self) -> dict:
        return json.loads(json.dumps(self.state))
