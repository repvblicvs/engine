"""Daily primary-source collection and bounded trusted-operator obligations."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import time
import uuid
from zoneinfo import ZoneInfo
from .source_collection import DEFAULT_FEEDS
from .store import ConflictError, LeaseError, encode

TIMEZONE = ZoneInfo("America/New_York")
PENDING = {"queued", "running", "deferred", "blocked"}


def tick(store, now: datetime | None = None) -> dict:
    """Enqueue once per Eastern day after 06:00; no model calls or HTTP here.

    A process lock serializes the pending-operator check with its submission.
    Operators resolve obligations through the same store/frontends. Unknown
    operator_review work is deferred by the deterministic worker, never improvised.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Scheduler time must include a timezone")
    local = current.astimezone(TIMEZONE)
    if local.hour < 6 or store.control() != "running":
        return {"scheduled": False, "reason": "before_daily_window_or_engine_not_running"}
    directory = store.root / "locks"
    directory.mkdir(exist_ok=True, mode=0o700)
    descriptor = os.open(directory / "scheduler.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"scheduled": False, "reason": "another_scheduler_is_active"}
        if store.control() != "running":
            return {"scheduled": False, "reason": "engine_not_running"}
        date = local.date().isoformat()
        source = store.enqueue({"kind": "source_refresh", "payload": {"feeds": list(DEFAULT_FEEDS), "source_date": date}, "priority": 80, "max_attempts": 3}, f"daily-source-refresh:{date}")
        pending = next((task for task in store.list_tasks() if task["kind"] == "operator_review" and task["status"] in PENDING), None)
        if not pending:
            pending = store.enqueue({"kind": "operator_review", "payload": {"source_task_id": source["id"], "source_date": date, "purpose": "Resolve accepted customer obligations first; review authorized inboxes and source candidates; qualify legitimate work, scope offers and delivery, and record completed actions and receipts. Unknown AI permission or eligibility requires customer/source review. Independent R&D must remain at most10%capacity."}, "priority": 100, "max_attempts": 1}, f"daily-operator-review:{date}")
        return {"scheduled": True, "source_task_id": source["id"], "operator_task_id": pending["id"], "source_date": date}
    finally:
        os.close(descriptor)


def _operator_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS operator_receipts(
        receipt_id TEXT PRIMARY KEY, operator_id TEXT NOT NULL, task_id TEXT NOT NULL,
        token TEXT NOT NULL, status TEXT NOT NULL, request_hash TEXT NOT NULL,
        content_hash TEXT, evidence TEXT, result TEXT, created REAL NOT NULL,updated REAL NOT NULL)""")


def _identity(operator_id, receipt_id):
    if not isinstance(operator_id, str) or not operator_id.strip() or len(operator_id) > 100 or not isinstance(receipt_id, str) or not receipt_id.strip() or len(receipt_id) > 256:
        raise ValueError("Explicit operator_id and bounded receipt_id are required")


def _owner(operator_id, receipt_id):
    return "operator:" + hashlib.sha256((operator_id + "\n" + receipt_id).encode()).hexdigest()


def claim_operator(store, operator_id: str, receipt_id: str, task_id: str | None = None, lease_seconds: float = 300) -> dict | None:
    """Claim only an operator_review obligation through a trusted frontend.

    Operator identity labels provenance; the returned random lease token fences
    callbacks. Connector account authorization is supplied by the invoking host,
    not inferred or cryptographically authenticated by this local bridge.
    """
    _identity(operator_id, receipt_id)
    if not isinstance(lease_seconds, (int, float)) or not 1 <= lease_seconds <= 3600:
        raise ValueError("Operator lease must be 1–3600 seconds")
    fingerprint = hashlib.sha256(encode({"operator_id": operator_id, "task_id": task_id, "lease_seconds": float(lease_seconds)}).encode()).hexdigest()
    owner, now = _owner(operator_id, receipt_id), time.time()
    with store.connection(write=True) as db:
        _operator_schema(db)
        previous = db.execute("SELECT * FROM operator_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
        if previous:
            if previous["request_hash"] != fingerprint or previous["operator_id"] != operator_id:
                raise ConflictError("Operator receipt_id reused with different claim parameters")
            task = db.execute("SELECT * FROM tasks WHERE id=?", (previous["task_id"],)).fetchone()
            if previous["status"] == "completed":
                return {"receipt_id": receipt_id, "operator_id": operator_id, "task": store._task(task), "status": "completed", "content_hash": previous["content_hash"]}
            if previous["status"] != "claimed":
                raise ConflictError("Previous operator receipt is closed; use a new receipt_id")
            store._owned(db, previous["task_id"], owner)
            return {"receipt_id": receipt_id, "operator_id": operator_id, "task": store._task(task), "lease_token": previous["token"], "status": "claimed"}
        store._recover(db, now)
        if db.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0] != "running":
            return None
        if db.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0] >= 2:
            return None
        if task_id:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task["kind"] != "operator_review":
                raise ValueError("Operator bridge accepts operator_review tasks only")
            if task["status"] not in {"queued", "deferred"}:
                return None
        else:
            task = db.execute("SELECT * FROM tasks WHERE kind='operator_review' AND status IN ('queued','deferred') ORDER BY priority DESC,created LIMIT 1").fetchone()
            if not task:
                return None
        token = uuid.uuid4().hex
        db.execute("UPDATE tasks SET status='running',attempts=attempts+1,lease_owner=?,lease_expires=?,updated=? WHERE id=?", (owner, now + lease_seconds, now, task["id"]))
        db.execute("INSERT INTO attempts(task_id,number,owner,started) VALUES(?,?,?,?)", (task["id"], task["attempts"] + 1, owner, now))
        db.execute("INSERT INTO operator_receipts VALUES(?,?,?,?,'claimed',?,NULL,NULL,NULL,?,?)", (receipt_id, operator_id, task["id"], token, fingerprint, now, now))
        store._event(db, "operator_claimed", task["id"], operator_id=operator_id, receipt_id=receipt_id)
        return {"receipt_id": receipt_id, "operator_id": operator_id, "task": store._task(db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()), "lease_token": token, "status": "claimed"}


def _receipt(db, operator_id, receipt_id, lease_token):
    _identity(operator_id, receipt_id)
    row = db.execute("SELECT * FROM operator_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
    if not row or row["operator_id"] != operator_id or row["token"] != lease_token:
        raise LeaseError("Operator identity or lease token does not match the receipt")
    return row


def _complete_operator(store, operator_id: str, receipt_id: str, lease_token: str, evidence: dict, result: dict) -> dict:
    """Commit a bounded evidence-bearing operator receipt without sending anything."""
    if not isinstance(evidence, dict) or not evidence or len(encode(evidence).encode()) > 64000 or not isinstance(result, dict) or len(encode(result).encode()) > 32000:
        raise ValueError("Nonempty evidence up to64KB and result object up to32KB are required")
    source_refs, outbox_receipts = evidence.get("source_refs", []), evidence.get("outbox_receipts", [])
    if not isinstance(source_refs, list) or len(source_refs) > 50 or not all(isinstance(item, str) and item for item in source_refs) or not isinstance(outbox_receipts, list) or len(outbox_receipts) > 20 or not all(isinstance(item, str) and item for item in outbox_receipts):
        raise ValueError("source_refs and outbox_receipts must be bounded lists of strings")
    if not source_refs and not outbox_receipts:
        raise ValueError("Operator completion needs inspected source references or confirmed outbox receipts")
    digest = hashlib.sha256(encode({"operator_id": operator_id, "receipt_id": receipt_id, "evidence": evidence, "result": result}).encode()).hexdigest()
    with store.connection(write=True) as db:
        _operator_schema(db)
        receipt = _receipt(db, operator_id, receipt_id, lease_token)
        if receipt["status"] == "completed":
            if receipt["content_hash"] != digest:
                raise ConflictError("Completed receipt content differs")
            return {"task_id": receipt["task_id"], "receipt_id": receipt_id, "content_hash": digest, "status": "completed"}
        if receipt["status"] != "claimed":
            raise ConflictError("Operator receipt is already closed")
        task = store._owned(db, receipt["task_id"], _owner(operator_id, receipt_id))
        for action_id in outbox_receipts:
            action = db.execute("SELECT status,external_ref FROM outbox WHERE id=?", (action_id,)).fetchone()
            if not action or action["status"] != "delivered" or not action["external_ref"]:
                raise ValueError("Outbox receipt lacks confirmed external delivery")
        now = time.time()
        task_result = result | {"operator_receipt": receipt_id, "operator_id": operator_id, "receipt_hash": digest, "evidence": evidence}
        db.execute("UPDATE tasks SET status='completed',result=?,error=NULL,lease_owner=NULL,lease_expires=NULL,updated=? WHERE id=?", (encode(task_result), now, receipt["task_id"]))
        db.execute("UPDATE attempts SET ended=?,outcome='completed' WHERE task_id=? AND number=?", (now, receipt["task_id"], task["attempts"]))
        db.execute("UPDATE operator_receipts SET status='completed',content_hash=?,evidence=?,result=?,updated=? WHERE receipt_id=?", (digest, encode(evidence), encode(result), now, receipt_id))
        store._event(db, "operator_completed", receipt["task_id"], operator_id=operator_id, receipt_id=receipt_id, content_hash=digest)
        return {"task_id": receipt["task_id"], "receipt_id": receipt_id, "content_hash": digest, "status": "completed"}


def complete_operator(store, operator_id: str, receipt_id: str, lease_token: str, evidence: dict, result: dict) -> dict:
    """Complete the trusted review and idempotently record its observed capacity.

    Capacity is claim-to-verified-completion wall time, including connector waits,
    not an invoice, CPU-time claim, or independently verified customer payment.
    A replay completes missing accounting without replaying connector actions.
    """
    response = _complete_operator(store, operator_id, receipt_id, lease_token, evidence, result)
    with store.connection() as db:
        receipt = db.execute("SELECT created,updated FROM operator_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
    elapsed = max(0.0, receipt["updated"] - receipt["created"])
    from .commercial import Commerce
    commerce = Commerce(store)
    execution_id = "operator:" + hashlib.sha256((operator_id + "\n" + receipt_id).encode()).hexdigest()
    commerce.reserve_execution(execution_id, "acquisition", max(0.000001, elapsed))
    commerce.complete_execution(execution_id, elapsed)
    return response


def defer_operator(store, operator_id: str, receipt_id: str, lease_token: str, reason: str) -> dict:
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise ValueError("A bounded defer reason is required")
    with store.connection(write=True) as db:
        _operator_schema(db)
        receipt = _receipt(db, operator_id, receipt_id, lease_token)
        if receipt["status"] != "claimed":
            raise ConflictError("Operator receipt is already closed")
        task = store._owned(db, receipt["task_id"], _owner(operator_id, receipt_id))
        now = time.time()
        db.execute("UPDATE tasks SET status='deferred',error=?,lease_owner=NULL,lease_expires=NULL,updated=?,next_run=? WHERE id=?", (reason, now, now + 60, task["id"]))
        db.execute("UPDATE attempts SET ended=?,outcome='deferred' WHERE task_id=? AND number=?", (now, task["id"], task["attempts"]))
        db.execute("UPDATE operator_receipts SET status='deferred',result=?,updated=? WHERE receipt_id=?", (encode({"reason": reason}), now, receipt_id))
        store._event(db, "operator_deferred", task["id"], operator_id=operator_id, receipt_id=receipt_id, reason=reason)
        return {"task_id": task["id"], "receipt_id": receipt_id, "status": "deferred"}


def renew_operator(store, operator_id: str, receipt_id: str, lease_token: str, lease_seconds: float = 300) -> dict:
    if not isinstance(lease_seconds, (int, float)) or not 1 <= lease_seconds <= 3600:
        raise ValueError("Operator lease must be1–3600seconds")
    with store.connection(write=True) as db:
        _operator_schema(db)
        receipt = _receipt(db, operator_id, receipt_id, lease_token)
        if receipt["status"] != "claimed":
            raise ConflictError("Operator receipt is already closed")
        store._owned(db, receipt["task_id"], _owner(operator_id, receipt_id))
        until = time.time() + lease_seconds
        db.execute("UPDATE tasks SET lease_expires=? WHERE id=?", (until, receipt["task_id"]))
        return {"task_id": receipt["task_id"], "receipt_id": receipt_id, "lease_expires": until}
