"""Private SQLite execution state shared by every frontend.

Each mutation is committed atomically. Claiming work increments its attempt count;
expired leases are recovered before the next claim. External actions are an outbox
of reviewable intentions, never permission to issue arbitrary shell commands.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any


def state_directory() -> Path:
    configured = os.environ.get("REPVBLICVS_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Repvblicvs/engine"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "repvblicvs-engine"


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ConflictError(ValueError):
    """An idempotency identifier was reused for a different instruction."""


class LeaseError(RuntimeError):
    """A stale or different worker attempted to mutate leased work."""


class Store:
    """Durable state. ``root`` is private runtime storage, outside source control.

    Task specs support kind, payload, priority (larger first), dependencies, and
    max_attempts (1..10). Results and checkpoints must be JSON serializable.
    """

    def __init__(self, root: str | Path | None = None):
        # SQLite WAL/SHM and every private artifact must be private from creation.
        os.umask(0o077)
        self.root = Path(root).expanduser().resolve() if root else state_directory()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir(exist_ok=True, mode=0o700)
        self.path = self.root / "engine.sqlite3"
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO meta VALUES('control','running');
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY, request_id TEXT UNIQUE, spec_hash TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, priority INTEGER NOT NULL,
                    dependencies TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    next_run REAL NOT NULL, lease_owner TEXT, lease_expires REAL,
                    checkpoint TEXT, result TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS runnable ON tasks(status,next_run,priority);
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
                    task_id TEXT, kind TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts(
                    task_id TEXT NOT NULL, number INTEGER NOT NULL, owner TEXT NOT NULL,
                    started REAL NOT NULL, ended REAL, outcome TEXT,
                    PRIMARY KEY(task_id,number));
                CREATE TABLE IF NOT EXISTS costs(
                    request_id TEXT PRIMARY KEY, task_id TEXT, provider TEXT NOT NULL,
                    account_id TEXT NOT NULL, amount REAL NOT NULL CHECK(amount>=0),
                    included INTEGER NOT NULL, time REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(
                    id TEXT PRIMARY KEY, action_key TEXT UNIQUE NOT NULL, task_id TEXT,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                    external_ref TEXT, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts(
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, evidence TEXT NOT NULL,
                    verified_until REAL NOT NULL, remaining_calls INTEGER,
                    available_credit REAL NOT NULL DEFAULT 0, reset_at REAL);
                CREATE TABLE IF NOT EXISTS providers(
                    name TEXT PRIMARY KEY, account_id TEXT NOT NULL, model TEXT NOT NULL,
                    status TEXT NOT NULL, evidence TEXT NOT NULL, verified_until REAL NOT NULL,
                    budget REAL NOT NULL DEFAULT 0, included_only INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS reservations(
                    request_id TEXT PRIMARY KEY, provider TEXT NOT NULL,
                    account_id TEXT NOT NULL, ceiling REAL NOT NULL,
                    status TEXT NOT NULL, actual REAL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS write_leases(
                    path TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
            """)
            if "retry_count" not in {row[1] for row in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
            if "retry_after" not in {row[1] for row in db.execute("PRAGMA table_info(providers)")}:
                db.execute("ALTER TABLE providers ADD COLUMN retry_after REAL NOT NULL DEFAULT 0")
            if "request_hash" not in {row[1] for row in db.execute("PRAGMA table_info(reservations)")}:
                db.execute("ALTER TABLE reservations ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self, *, write: bool = False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, event_kind: str, task_id: str | None = None, **data):
        db.execute("INSERT INTO events(time,task_id,kind,data) VALUES(?,?,?,?)", (time.time(), task_id, event_kind, encode(data)))

    @staticmethod
    def _task(row):
        if row is None:
            raise KeyError("Task not found")
        task = dict(row)
        for key in ("payload", "dependencies", "checkpoint", "result"):
            task[key] = json.loads(task[key]) if task[key] is not None else None
        task.pop("spec_hash", None)
        return task

    def enqueue(self, spec: dict, request_id: str | None = None) -> dict:
        if not isinstance(spec, dict) or not isinstance(spec.get("kind"), str) or not spec["kind"]:
            raise ValueError("A nonempty task kind is required")
        allowed = {"kind", "payload", "priority", "dependencies", "max_attempts"}
        if set(spec) - allowed:
            raise ValueError("Unknown task fields: " + ", ".join(sorted(set(spec) - allowed)))
        payload = spec.get("payload", {})
        deps = spec.get("dependencies", [])
        priority, maximum = spec.get("priority", 0), spec.get("max_attempts", 3)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        if not isinstance(priority, int) or isinstance(priority, bool) or not -1000 <= priority <= 1000:
            raise ValueError("priority must be an integer from -1000 to 1000")
        if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 10:
            raise ValueError("max_attempts must be an integer from 1 to 10")
        if not isinstance(deps, list) or not all(isinstance(item, str) for item in deps):
            raise ValueError("dependencies must be a list of task IDs")
        if request_id is not None and (not isinstance(request_id, str) or not request_id or len(request_id) > 512):
            raise ValueError("request_id must be a nonempty string up to 512 characters")
        normalized = {"kind": spec["kind"], "payload": payload, "priority": priority, "dependencies": sorted(set(deps)), "max_attempts": maximum}
        serialized = encode(normalized)
        if len(serialized.encode()) > 2_000_000:
            raise ValueError("Task specification exceeds 2 MB")
        fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
        now, task_id = time.time(), uuid.uuid4().hex
        with self.connection(write=True) as db:
            if request_id:
                previous = db.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
                if previous:
                    if previous["spec_hash"] != fingerprint:
                        raise ConflictError("request_id already identifies a different task")
                    return self._task(previous)
            for dep in normalized["dependencies"]:
                if not db.execute("SELECT 1 FROM tasks WHERE id=?", (dep,)).fetchone():
                    raise ValueError(f"Unknown dependency: {dep}")
            db.execute("""INSERT INTO tasks(id,request_id,spec_hash,kind,payload,priority,dependencies,status,attempts,max_attempts,created,updated,next_run)
                VALUES(?,?,?,?,?,?,?,'queued',0,?,?,?,?)""", (task_id, request_id, fingerprint, spec["kind"], encode(payload), priority, encode(normalized["dependencies"]), maximum, now, now, now))
            self._event(db, "task_submitted", task_id, kind=spec["kind"], priority=priority)
            return self._task(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def get_task(self, task_id: str) -> dict:
        with self.connection() as db:
            return self._task(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def list_tasks(self) -> list:
        with self.connection() as db:
            return [self._task(row) for row in db.execute("SELECT * FROM tasks ORDER BY priority DESC,created,id")]

    def events(self, after_id: int = 0) -> list:
        with self.connection() as db:
            return [dict(row) | {"data": json.loads(row["data"])} for row in db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 1000", (after_id,))]

    def control(self, mode: str | None = None) -> str:
        if mode is not None and mode not in {"running", "paused", "stopped"}:
            raise ValueError("Control must be running, paused, or stopped")
        with self.connection(write=mode is not None) as db:
            if mode:
                db.execute("UPDATE meta SET value=? WHERE key='control'", (mode,))
                self._event(db, "control_changed", mode=mode)
            return db.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0]

    def pause(self):
        return self.control("paused")

    def resume(self):
        return self.control("running")

    def stop(self):
        return self.control("stopped")

    def set_priority(self, task_id: str, priority: int) -> dict:
        if not isinstance(priority, int) or isinstance(priority, bool) or not -1000 <= priority <= 1000:
            raise ValueError("priority must be an integer from -1000 to 1000")
        with self.connection(write=True) as db:
            if not db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise KeyError("Task not found")
            db.execute("UPDATE tasks SET priority=?,updated=? WHERE id=?", (priority, time.time(), task_id))
            self._event(db, "priority_changed", task_id, priority=priority)
        return self.get_task(task_id)

    def _recover(self, db, now):
        for row in db.execute("SELECT * FROM tasks WHERE status='running' AND lease_expires<=?", (now,)).fetchall():
            if row["kind"] == "operator_review":
                db.execute("UPDATE tasks SET status='deferred',lease_owner=NULL,lease_expires=NULL,updated=?,next_run=?,error='Operator lease expired; verify receipts before continuing' WHERE id=?", (now, now + 60, row["id"]))
                db.execute("UPDATE attempts SET ended=?,outcome='lease_expired' WHERE task_id=? AND number=?", (now, row["id"], row["attempts"]))
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operator_receipts'").fetchone():
                    db.execute("UPDATE operator_receipts SET status='expired',updated=? WHERE task_id=? AND status='claimed'", (now, row["id"]))
                self._event(db, "operator_lease_expired", row["id"])
                continue
            status = "queued" if row["retry_count"] + 1 < row["max_attempts"] else "failed"
            db.execute("UPDATE tasks SET status=?,retry_count=retry_count+1,lease_owner=NULL,lease_expires=NULL,updated=?,next_run=?,error=? WHERE id=?", (status, now, now, "Worker lease expired", row["id"]))
            db.execute("UPDATE attempts SET ended=?,outcome='lease_expired' WHERE task_id=? AND number=?", (now, row["id"], row["attempts"]))
            self._event(db, "lease_recovered", row["id"], status=status, checkpoint_retained=row["checkpoint"] is not None)

    def recover(self) -> None:
        with self.connection(write=True) as db:
            self._recover(db, time.time())

    def claim(self, owner: str, lease_seconds: float = 60, *, exclude_kinds: tuple[str, ...] = ()) -> dict | None:
        if not owner or not 0 < lease_seconds <= 3600:
            raise ValueError("Owner and a lease between zero and 3600 seconds required")
        now = time.time()
        with self.connection(write=True) as db:
            self._recover(db, now)
            if db.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0] != "running":
                return None
            if db.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0] >= 2:
                return None
            for row in db.execute("SELECT * FROM tasks WHERE status='queued' AND next_run<=? ORDER BY priority DESC,created,id", (now,)).fetchall():
                if row["kind"] in exclude_kinds:
                    continue
                dependencies = json.loads(row["dependencies"])
                statuses = [db.execute("SELECT status FROM tasks WHERE id=?", (dep,)).fetchone()[0] for dep in dependencies]
                if any(status in {"failed", "blocked"} for status in statuses):
                    db.execute("UPDATE tasks SET status='blocked',error='A dependency failed',updated=? WHERE id=?", (now, row["id"]))
                    self._event(db, "dependency_blocked", row["id"])
                    continue
                if any(status != "completed" for status in statuses):
                    continue
                db.execute("UPDATE tasks SET status='running',attempts=attempts+1,updated=?,lease_owner=?,lease_expires=? WHERE id=?", (now, owner, now + lease_seconds, row["id"]))
                db.execute("INSERT INTO attempts(task_id,number,owner,started) VALUES(?,?,?,?)", (row["id"], row["attempts"] + 1, owner, now))
                self._event(db, "task_claimed", row["id"], owner=owner, attempt=row["attempts"] + 1)
                return self._task(db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone())
            return None

    @staticmethod
    def _owned(db, task_id, owner):
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row or row["status"] != "running" or row["lease_owner"] != owner or row["lease_expires"] <= time.time():
            raise LeaseError("Task is not currently leased to this worker")
        return row

    def renew(self, task_id: str, owner: str, lease_seconds: float = 60):
        if not 0 < lease_seconds <= 3600:
            raise ValueError("Invalid lease duration")
        with self.connection(write=True) as db:
            self._owned(db, task_id, owner)
            db.execute("UPDATE tasks SET lease_expires=? WHERE id=?", (time.time() + lease_seconds, task_id))

    def checkpoint(self, task_id: str, owner: str, value: dict):
        with self.connection(write=True) as db:
            self._owned(db, task_id, owner)
            db.execute("UPDATE tasks SET checkpoint=?,updated=? WHERE id=?", (encode(value), time.time(), task_id))
            self._event(db, "checkpoint", task_id)

    def complete(self, task_id: str, owner: str, result: Any) -> dict:
        now = time.time()
        with self.connection(write=True) as db:
            row = self._owned(db, task_id, owner)
            db.execute("UPDATE tasks SET status='completed',result=?,error=NULL,lease_owner=NULL,lease_expires=NULL,updated=? WHERE id=?", (encode(result), now, task_id))
            db.execute("UPDATE attempts SET ended=?,outcome='completed' WHERE task_id=? AND number=?", (now, task_id, row["attempts"]))
            self._event(db, "task_completed", task_id)
        return self.get_task(task_id)

    def fail(self, task_id: str, owner: str, error: str, *, retryable: bool = True) -> dict:
        now = time.time()
        with self.connection(write=True) as db:
            row = self._owned(db, task_id, owner)
            status = "queued" if retryable and row["retry_count"] + 1 < row["max_attempts"] else "failed"
            delay = min(300, 2 ** (row["retry_count"] + 1)) if status == "queued" else 0
            db.execute("UPDATE tasks SET status=?,retry_count=retry_count+1,error=?,lease_owner=NULL,lease_expires=NULL,updated=?,next_run=? WHERE id=?", (status, str(error)[:4000], now, now + delay, task_id))
            db.execute("UPDATE attempts SET ended=?,outcome=? WHERE task_id=? AND number=?", (now, status, task_id, row["attempts"]))
            self._event(db, "task_failed", task_id, retry=status == "queued", delay=delay)
        return self.get_task(task_id)

    def defer(self, task_id: str, owner: str, reason: str) -> dict:
        """Wait for an unavailable capability without consuming repeated attempts."""
        with self.connection(write=True) as db:
            row = self._owned(db, task_id, owner)
            now = time.time()
            db.execute("UPDATE tasks SET status='deferred',error=?,lease_owner=NULL,lease_expires=NULL,updated=?,next_run=? WHERE id=?", (reason[:4000], now, now + 60, task_id))
            db.execute("UPDATE attempts SET ended=?,outcome='deferred' WHERE task_id=? AND number=?", (now, task_id, row["attempts"]))
            self._event(db, "task_deferred", task_id, reason=reason[:4000])
        return self.get_task(task_id)

    def retry_deferred(self, task_id: str) -> dict:
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["status"] != "deferred":
                raise ValueError("Only a deferred task can be retried this way")
            now = time.time()
            db.execute("UPDATE tasks SET status='queued',error=NULL,next_run=?,updated=? WHERE id=?", (now, now, task_id))
            self._event(db, "task_resumed", task_id)
        return self.get_task(task_id)

    def revisit_deferred(self):
        """Bounded revisit of model allowance gaps; unsupported workflows stay deferred."""
        with self.connection(write=True) as db:
            now = time.time()
            for row in db.execute("SELECT id,error FROM tasks WHERE status='deferred' AND kind='model_instruction' AND next_run<=? LIMIT 2", (now,)).fetchall():
                if "ambiguous" in (row["error"] or "").lower() or "reconciliation" in (row["error"] or "").lower():
                    continue
                db.execute("UPDATE tasks SET status='queued',updated=? WHERE id=?", (now, row["id"]))
                self._event(db, "capability_revisited", row["id"])

    def record_cost(self, provider: str, account_id: str, amount: float, request_id: str, *, task_id: str | None = None, included: bool = True) -> dict:
        if not isinstance(amount, (int, float)) or amount < 0 or not request_id:
            raise ValueError("Nonnegative amount and request_id required")
        encode(amount)
        values = (task_id, provider, account_id, float(amount), int(included))
        with self.connection(write=True) as db:
            previous = db.execute("SELECT * FROM costs WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if tuple(previous[key] for key in ("task_id", "provider", "account_id", "amount", "included")) != values:
                    raise ConflictError("Cost request_id reused")
                return dict(previous)
            db.execute("INSERT INTO costs VALUES(?,?,?,?,?,?,?)", (request_id, *values, time.time()))
            self._event(db, "cost_recorded", task_id, provider=provider, amount=amount, included=included)
            return dict(db.execute("SELECT * FROM costs WHERE request_id=?", (request_id,)).fetchone())

    def prepare_action(self, action_key: str, kind: str, payload: dict, task_id: str | None = None) -> dict:
        """Deduplicate external intentions; execution requires a separate verified adapter."""
        now, action_id = time.time(), uuid.uuid4().hex
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM outbox WHERE action_key=?", (action_key,)).fetchone()
            if row:
                if row["kind"] != kind or row["payload"] != encode(payload) or row["task_id"] != task_id:
                    raise ConflictError("Action key reused for different content")
            else:
                db.execute("INSERT INTO outbox VALUES(?,?,?,?,?,'prepared',NULL,?,?)", (action_id, action_key, task_id, kind, encode(payload), now, now))
                self._event(db, "action_prepared", task_id, action_id=action_id, kind=kind)
                row = db.execute("SELECT * FROM outbox WHERE id=?", (action_id,)).fetchone()
            return dict(row) | {"payload": json.loads(row["payload"])}

    def complete_action(self, action_id: str, external_ref: str | None = None, *, uncertain: bool = False):
        """Uncertain outcomes are reconciled rather than automatically replayed."""
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM outbox WHERE id=?", (action_id,)).fetchone()
            if not row:
                raise KeyError("Action not found")
            status = "unknown" if uncertain else "delivered"
            if row["status"] == "delivered":
                if status != "delivered" or row["external_ref"] != external_ref:
                    raise ConflictError("Delivered action cannot be replayed or replaced")
                return
            db.execute("UPDATE outbox SET status=?,external_ref=?,updated=? WHERE id=?", (status, external_ref, time.time(), action_id))
            self._event(db, "action_updated", row["task_id"], action_id=action_id, status=status)

    def begin_action(self, action_id: str) -> dict:
        """Durably fence an intention before a verified adapter makes its send.

        A recovered dispatching action is ambiguous, never automatically resent.
        The adapter must use action_key as the provider's idempotency identifier.
        """
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM outbox WHERE id=?", (action_id,)).fetchone()
            if not row:
                raise KeyError("Action not found")
            if row["status"] != "prepared":
                raise ConflictError("Action already dispatched or needs reconciliation")
            db.execute("UPDATE outbox SET status='dispatching',updated=? WHERE id=?", (time.time(), action_id))
            self._event(db, "action_dispatching", row["task_id"], action_id=action_id)
            return dict(row) | {"payload": json.loads(row["payload"]), "status": "dispatching"}

    def reconcile_actions(self):
        """On adapter restart, unknown sends must be reconciled with receipts."""
        with self.connection(write=True) as db:
            for row in db.execute("SELECT id,task_id FROM outbox WHERE status='dispatching'").fetchall():
                db.execute("UPDATE outbox SET status='unknown',updated=? WHERE id=?", (time.time(), row["id"]))
                self._event(db, "action_ambiguous", row["task_id"], action_id=row["id"])

    def list_actions(self) -> list:
        with self.connection() as db:
            return [dict(row) | {"payload": json.loads(row["payload"])} for row in db.execute("SELECT * FROM outbox ORDER BY created")]

    def artifacts(self, task_id: str | None = None) -> list:
        root = self.artifact_root
        if task_id:
            self.get_task(task_id)
            root = root / task_id
        return [{"path": str(path.relative_to(self.artifact_root)), "size": path.stat().st_size} for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()]

    def status(self) -> dict:
        with self.connection() as db:
            counts = {row[0]: row[1] for row in db.execute("SELECT status,count(*) FROM tasks GROUP BY status")}
            spend = db.execute("SELECT COALESCE(sum(amount),0) FROM costs WHERE included=0").fetchone()[0]
        return {"control": self.control(), "task_counts": counts, "recorded_paid_cost": spend, "state_dir": str(self.root)}

    def acquire_write_leases(self, owner: str, paths: list[str], lease_seconds: float = 300) -> list:
        """Serialize overlapping file/directory ownership for trusted adapters.

        Owning a lease supplies coordination, not permission to read/write the path.
        Parent and descendant paths conflict even when strings differ.
        """
        if not owner or not paths or not 0 < lease_seconds <= 3600 or not all(isinstance(path, str) and path for path in paths):
            raise ValueError("Owner, paths, and a bounded lease duration are required")
        normalized = sorted({str(Path(path).expanduser().resolve()) for path in paths})
        now = time.time()
        with self.connection(write=True) as db:
            db.execute("DELETE FROM write_leases WHERE expires<=?", (now,))
            existing = db.execute("SELECT * FROM write_leases").fetchall()
            for path in normalized:
                target = Path(path)
                for row in existing:
                    other = Path(row["path"])
                    if row["owner"] != owner and (target.is_relative_to(other) or other.is_relative_to(target)):
                        raise LeaseError("A conflicting writer owns this path")
            for path in normalized:
                db.execute("INSERT INTO write_leases VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET owner=excluded.owner,expires=excluded.expires", (path, owner, now + lease_seconds))
            self._event(db, "write_leases_acquired", owner=owner, count=len(normalized))
            return [{"path": path, "owner": owner, "expires": now + lease_seconds} for path in normalized]

    def release_write_leases(self, owner: str):
        with self.connection(write=True) as db:
            db.execute("DELETE FROM write_leases WHERE owner=?", (owner,))
            self._event(db, "write_leases_released", owner=owner)
