"""Trusted connector operator interface over the canonical store.

The operator may be a desktop agent with existing Gmail/Stripe connectors.
This interface reserves each external attempt before releasing its payload.
It never treats a callback timeout as permission to repeat an external action.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import uuid
from datetime import datetime, timezone
from .commercial import Commerce, CommercialError
from .store import Store, encode


class ConnectorBridge:
    def __init__(self, store: Store):
        self.store, self.commerce = store, Commerce(store)
        with store.connection(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS connector_attempts(action_id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, operator TEXT NOT NULL, created REAL NOT NULL, receipt TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS connector_receipts(token TEXT NOT NULL, receipt TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(token,receipt))")

    def begin(self, action_id: str, operator: str) -> dict:
        if not isinstance(operator, str) or not operator.strip() or len(operator) > 100:
            raise ValueError("A bounded trusted operator identity is required")
        token = uuid.uuid4().hex
        with self.store.connection(write=True) as db:
            action = db.execute("SELECT * FROM outbox WHERE id=?", (action_id,)).fetchone()
            if action is None: raise KeyError("Unknown commercial action")
            existing = db.execute("SELECT * FROM connector_attempts WHERE action_id=?", (action_id,)).fetchone()
            if existing or action["status"] != "prepared":
                return {"execute": False, "action_id": action_id, "status": action["status"], "instruction": "Inspect the existing external receipt; do not repeat this action."}
            db.execute("INSERT INTO connector_attempts VALUES(?,?,?,?,NULL)", (action_id, token, operator, time.time()))
        capture = {}

        class Capture:
            def send(self, payload, *, idempotency_key):
                capture.update(payload=payload, idempotency_key=idempotency_key)
                return {"status": "unknown"}
            invoice = send

        try:
            action = self.commerce.dispatch(action_id, Capture())
        except Exception:
            # Validation failed before any payload was released or external call.
            with self.store.connection(write=True) as db:
                db.execute("DELETE FROM connector_attempts WHERE action_id=? AND token=?", (action_id, token))
            raise
        if not capture:
            return {"execute": False, "action_id": action_id, "status": action["status"]}
        return {"execute": True, "action_id": action_id, "attempt_token": token, "kind": action["kind"], **capture, "instruction": "Use the existing authorized business connector once. Persist its actual receipt immediately. If the call outcome is unknown, inspect external state rather than resending."}

    def receipt(self, action_id: str, token: str, receipt: dict) -> dict:
        if not isinstance(receipt, dict) or set(receipt) - {"status", "external_ref", "evidence"}:
            raise ValueError("Receipt supports status, external_ref and evidence only")
        if receipt.get("status") not in {"confirmed", "unknown"}:
            raise ValueError("This bridge accepts confirmed or unknown outcomes; absence requires independent reconciliation")
        if not isinstance(receipt.get("evidence"), str) or not receipt["evidence"].strip():
            raise ValueError("A real connector observation is required")
        if receipt["status"] == "confirmed" and (not isinstance(receipt.get("external_ref"), str) or not receipt["external_ref"].strip()):
            raise ValueError("Confirmed actions require the connector's external identifier")
        with self.store.connection(write=True) as db:
            attempt = db.execute("SELECT * FROM connector_attempts WHERE action_id=? AND token=?", (action_id, token)).fetchone()
            if not attempt: raise ValueError("Unknown connector attempt")
            encoded = encode(receipt)
            if attempt["receipt"] and json.loads(attempt["receipt"]).get("status") == "confirmed" and attempt["receipt"] != encoded:
                raise ValueError("An existing external receipt cannot be overwritten")
            db.execute("INSERT OR IGNORE INTO connector_receipts VALUES(?,?,?)", (token, encoded, time.time()))
            db.execute("UPDATE connector_attempts SET receipt=? WHERE action_id=?", (encoded, action_id))
        # A crash after persisting this receipt is safely replayable locally.
        return self.commerce._resolved(action_id, receipt, datetime.now(timezone.utc), reconciliation=True)


def operate(store: Store, request: dict) -> dict:
    if not isinstance(request, dict): raise ValueError("Operator request must be an object")
    action = request.get("operation")
    args = request.get("args", {})
    if not isinstance(args, dict): raise ValueError("args must be an object")
    bridge = ConnectorBridge(store)
    commerce = bridge.commerce
    if action == "snapshot": return commerce.snapshot()
    if action == "begin": return bridge.begin(**args)
    if action == "receipt": return bridge.receipt(**args)
    if action == "qualify":
        args = dict(args)
        args["available_capabilities"] = set(args.get("available_capabilities", []))
        return commerce.register_opportunity(**args)
    methods = {"solicitation": commerce.register_solicitation, "inquiry": commerce.prepare_inquiry,
               "application": commerce.prepare_application,
               "contact": commerce.prepare_contact, "quote": commerce.prepare_quote,
               "agreement": commerce.agree_scope, "invoice": commerce.prepare_invoice,
               "event": commerce.record_event, "merchant": commerce.set_merchant_readiness,
               "experiments": commerce.review_experiments, "revise": commerce.revise_experiment,
               "seed": commerce.seed_experiments}
    if action not in methods: raise ValueError("Unknown trusted operator operation")
    return methods[action](**args)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="JSON file or - for stdin")
    parser.add_argument("--state-dir")
    args = parser.parse_args(argv)
    try:
        value = sys.stdin.read() if args.request == "-" else Path(args.request).read_text()
        result = operate(Store(args.state_dir), json.loads(value))
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, TypeError, KeyError, RuntimeError, OSError, CommercialError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
