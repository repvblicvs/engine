"""Persisted routing gates, shared allowance accounting, and bounded reservations.

This module never authenticates, purchases credits, or invokes a model. A live
adapter may run only after a reservation succeeds using current verified evidence.
"""
from __future__ import annotations

import json
import hashlib
import time
from .store import Store, ConflictError, encode

ROUTE_ORDER = ("fable", "opus", "sol", "gemini", "copilot")
DEFAULT_MODELS = {"fable": "fable-5.1", "opus": "opus-5.5", "sol": "gpt-sol-6.1", "gemini": "gemini", "copilot": "copilot"}


class ProviderUnavailable(RuntimeError):
    pass


class Router:
    def __init__(self, store: Store):
        self.store = store

    def configure_account(self, account_id: str, *, evidence: dict, verified_until: float, remaining_calls: int | None = None, available_credit: float = 0, status: str = "verified", reset_at: float | None = None):
        """Evidence must describe a live allowance check; absence is unavailable."""
        if status not in {"verified", "unavailable", "exhausted"} or available_credit < 0 or (remaining_calls is not None and remaining_calls < 0):
            raise ValueError("Invalid account allowance")
        if status == "verified" and (not evidence or verified_until <= time.time()):
            raise ValueError("Current allowance evidence is required")
        with self.store.connection(write=True) as db:
            db.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,evidence=excluded.evidence,verified_until=excluded.verified_until,remaining_calls=excluded.remaining_calls,available_credit=excluded.available_credit,reset_at=excluded.reset_at", (account_id, status, encode(evidence), verified_until, remaining_calls, available_credit, reset_at))
            self.store._event(db, "account_verified", account_id=account_id, status=status)

    def configure_provider(self, name: str, account_id: str, *, model: str | None = None, evidence: dict | None = None, verified_until: float = 0, status: str = "unavailable", budget: float = 0, included_only: bool = True):
        """included_only retains the no-new-cash-route gate for non-Fable adapters.

        Owner-authorized Codex existing prepaid credits may pass that same gate;
        their actual billing_mode and credit quantities live in account_evidence.
        """
        if name not in ROUTE_ORDER or status not in {"ready", "unavailable", "cooldown"}:
            raise ValueError("Unknown provider or status")
        if budget < 0 or (name == "fable" and budget > 5):
            raise ValueError("Fable cumulative budget must not exceed $5")
        if name != "fable" and not included_only:
            raise ValueError("Only Fable's existing bounded balance permits paid calls")
        if status == "ready" and (not evidence or verified_until <= time.time()):
            raise ValueError("Ready requires current entitlement/model evidence")
        with self.store.connection(write=True) as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
                raise ValueError("Configure shared account allowance first")
            db.execute("INSERT INTO providers(name,account_id,model,status,evidence,verified_until,budget,included_only) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET account_id=excluded.account_id,model=excluded.model,status=excluded.status,evidence=excluded.evidence,verified_until=excluded.verified_until,budget=excluded.budget,included_only=excluded.included_only,retry_after=0", (name, account_id, model or DEFAULT_MODELS[name], status, encode(evidence or {}), verified_until, budget, int(included_only)))
            self.store._event(db, "provider_configured", provider=name, status=status)

    def list(self) -> list:
        now = time.time()
        with self.store.connection() as db:
            result = []
            for row in db.execute("SELECT p.*,a.status account_status,a.evidence account_evidence,a.verified_until allowance_until,a.remaining_calls,a.available_credit,a.reset_at FROM providers p JOIN accounts a ON p.account_id=a.id"):
                item = dict(row)
                item["evidence"] = json.loads(item["evidence"])
                item["account_evidence"] = json.loads(item["account_evidence"])
                item["billing_mode"] = item["account_evidence"].get("billing_mode", "unverified")
                item["currently_verified"] = row["status"] == "ready" and row["account_status"] == "verified" and min(row["verified_until"], row["allowance_until"]) > now
                item["effective_status"] = "ready" if item["currently_verified"] and (row["remaining_calls"] is None or row["remaining_calls"] > 0) else "unavailable"
                result.append(item)
        known = {item["name"] for item in result}
        result.extend({"name": name, "model": model, "status": "unavailable", "currently_verified": False, "reason": "No live account/allowance evidence configured"} for name, model in DEFAULT_MODELS.items() if name not in known)
        return sorted(result, key=lambda item: ROUTE_ORDER.index(item["name"]))

    def reserve(self, request_id: str, *, ceiling: float = 0, preferred: str | None = None) -> dict:
        """Select a permitted route atomically, accounting for shared account limits.

        A dollar ceiling > 0 is allowed only for Fable. Other routes have ceiling
        zero for new cash charges. Codex existing-credit observations are tracked
        separately in credit units, without an invented monetary conversion.
        Reservations remain charged against the safety envelope until settled or
        explicitly released after evidence that no external call occurred.
        """
        if not request_id or ceiling < 0 or ceiling > 5:
            raise ValueError("request_id and a ceiling from $0 to $5 required")
        if preferred is not None and preferred not in ROUTE_ORDER:
            raise ValueError("Unknown preferred provider")
        order = list(ROUTE_ORDER)
        if preferred:
            order.remove(preferred)
            order.insert(0, preferred)
        now = time.time()
        fingerprint = hashlib.sha256(encode({"ceiling": float(ceiling), "preferred": preferred}).encode()).hexdigest()
        with self.store.connection(write=True) as db:
            previous = db.execute("SELECT * FROM reservations WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous["request_hash"] and previous["request_hash"] != fingerprint:
                    raise ConflictError("Reservation request_id reused with different routing/budget parameters")
                provider = db.execute("SELECT model FROM providers WHERE name=?", (previous["provider"],)).fetchone()
                return dict(previous) | {"model": provider["model"] if provider else None}
            for name in order:
                row = db.execute("SELECT p.*,a.status account_status,a.verified_until allowance_until,a.remaining_calls,a.available_credit FROM providers p JOIN accounts a ON p.account_id=a.id WHERE p.name=?", (name,)).fetchone()
                if not row or row["status"] != "ready" or row["account_status"] != "verified" or min(row["verified_until"], row["allowance_until"]) <= now:
                    continue
                if row["remaining_calls"] is not None and row["remaining_calls"] <= 0:
                    continue
                amount = ceiling if name == "fable" else 0
                if name == "fable":
                    used = db.execute("SELECT COALESCE(sum(CASE WHEN status='settled' THEN actual ELSE ceiling END),0) FROM reservations WHERE provider='fable' AND status!='released'").fetchone()[0]
                    recorded = db.execute("SELECT COALESCE(sum(amount),0) FROM costs WHERE provider='fable'").fetchone()[0]
                    # Reservations and separately recorded cost are conservative:
                    # double accounting is safer than exceeding the owner's cap.
                    if amount <= 0 or used + recorded + amount > min(5, row["budget"]) or row["available_credit"] < amount:
                        continue
                    db.execute("UPDATE accounts SET available_credit=available_credit-? WHERE id=?", (amount, row["account_id"]))
                if row["remaining_calls"] is not None:
                    db.execute("UPDATE accounts SET remaining_calls=remaining_calls-1 WHERE id=?", (row["account_id"],))
                db.execute("INSERT INTO reservations(request_id,provider,account_id,ceiling,status,actual,created,request_hash) VALUES(?,?,?,?, 'reserved',NULL,?,?)", (request_id, name, row["account_id"], amount, now, fingerprint))
                self.store._event(db, "provider_reserved", provider=name, account_id=row["account_id"], ceiling=amount)
                return dict(db.execute("SELECT * FROM reservations WHERE request_id=?", (request_id,)).fetchone()) | {"model": row["model"]}
        raise ProviderUnavailable("No verified route has sufficient authorized allowance")

    def settle(self, request_id: str, actual: float):
        with self.store.connection(write=True) as db:
            row = db.execute("SELECT * FROM reservations WHERE request_id=?", (request_id,)).fetchone()
            if not row or actual < 0 or actual > row["ceiling"]:
                raise ValueError("Actual cost must remain within the reserved ceiling")
            if row["status"] == "settled":
                if row["actual"] != actual:
                    raise ConflictError("Reservation already settled differently")
                return
            if row["status"] != "reserved":
                raise ConflictError("Reservation is no longer active")
            db.execute("UPDATE reservations SET status='settled',actual=? WHERE request_id=?", (actual, request_id))
            db.execute("UPDATE accounts SET available_credit=available_credit+? WHERE id=?", (row["ceiling"] - actual, row["account_id"]))
            self.store._event(db, "provider_settled", provider=row["provider"], actual=actual)

    def release(self, request_id: str, *, evidence_no_call: str):
        if not evidence_no_call:
            raise ValueError("Evidence that no model request occurred is required")
        with self.store.connection(write=True) as db:
            row = db.execute("SELECT * FROM reservations WHERE request_id=?", (request_id,)).fetchone()
            if not row or row["status"] != "reserved":
                raise ConflictError("Only an active reservation can be released")
            db.execute("UPDATE reservations SET status='released' WHERE request_id=?", (request_id,))
            db.execute("UPDATE accounts SET available_credit=available_credit+?,remaining_calls=CASE WHEN remaining_calls IS NULL THEN NULL ELSE remaining_calls+1 END WHERE id=?", (row["ceiling"], row["account_id"]))
            self.store._event(db, "reservation_released", provider=row["provider"], evidence=evidence_no_call)

    def unavailable(self, name: str, reason: str, *, retry_after: float | None = None, shared_quota_exhausted: bool = False):
        with self.store.connection(write=True) as db:
            until = retry_after or time.time() + 60
            db.execute("UPDATE providers SET status='cooldown',retry_after=? WHERE name=?", (until, name))
            if shared_quota_exhausted:
                db.execute("UPDATE accounts SET status='exhausted',reset_at=? WHERE id=(SELECT account_id FROM providers WHERE name=?)", (until, name))
            self.store._event(db, "provider_unavailable", provider=name, reason=reason[:2000], retry_after=until)
