"""A supervised, checkpointed deterministic worker; model adapters are separate."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from .store import Store, LeaseError

RESEARCH_SECONDS = 5.0
DELIVERIES = {"csv_cleanup", "document_package", "json_preflight", "catalog_inspect", "software_fix"}


def execution_class(task: dict) -> str:
    """Classify by registered kind/priority, never payload instructions or labels."""
    if task["kind"] == "research_sequence":
        return "exploratory_research"
    if task["kind"] == "source_refresh":
        return "acquisition"
    if task["priority"] >= 1000:
        return "customer_delivery"
    if task["kind"] in DELIVERIES:
        return "revenue_product"
    if task["kind"] == "model_instruction" and task["priority"] >= 80:
        return "acquisition"
    if task["kind"] == "model_instruction" and task["priority"] >= 60:
        return "revenue_product"
    return "maintenance"


class ResearchTimeout(RuntimeError):
    code = "research_timeout"


def _bounded_research(payload, output_dir, slot_fd):
    """Fixed workflow in an isolated process; timeout retains partial evidence.

    Five seconds bounds the computation, not a promise that OS process cleanup
    has zero overhead. Measured overruns remain recorded and block further R&D.
    """
    script = """import json,sys
from pathlib import Path
from repvblicvs_engine.workflows import run_workflow
try:
    result=run_workflow('research_sequence',json.load(sys.stdin),Path(sys.argv[1]))
    print(json.dumps({'result':result},allow_nan=False))
except Exception as error:
    print(json.dumps({'error':{'code':getattr(error,'code','research_failed'),'message':str(error)}}))
    sys.exit(2)
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(output_dir)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, pass_fds=(slot_fd,))
    try:
        stdout, stderr = process.communicate(json.dumps(payload, allow_nan=False).encode(), timeout=RESEARCH_SECONDS)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise ResearchTimeout("Independent study exceeded its fixed five-second wall limit; partial evidence retained") from exc
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    if len(stdout) + len(stderr) > 1_000_000:
        raise ResearchTimeout("Independent study output exceeded its fixed response limit")
    try:
        response = json.loads(stdout)
    except ValueError as exc:
        raise RuntimeError("Independent study failed to return a valid result") from exc
    if process.returncode != 0 or "error" in response:
        from .workflows import WorkflowError
        detail = response.get("error", {})
        raise WorkflowError(detail.get("code", "research_failed"), detail.get("message", "Independent study failed"))
    return response["result"]


class Worker:
    def __init__(self, store: Store, *, lease_seconds: float = 60):
        self.store = store
        self.owner = f"worker-{os.getpid()}-{uuid.uuid4().hex[:10]}"
        self.lease_seconds = lease_seconds
        self.shutdown = threading.Event()

    def step(self) -> dict | None:
        self.store.revisit_deferred()
        directory = self.store.root / "locks"
        directory.mkdir(exist_ok=True, mode=0o700)
        slot = None
        for index in range(2):
            descriptor = os.open(directory / f"worker-{index}.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                slot = descriptor
                break
            except BlockingIOError:
                os.close(descriptor)
        if slot is None:
            return None
        try:
            return self._step_locked(slot)
        finally:
            os.close(slot)

    def _step_locked(self, slot_fd) -> dict | None:
        # Trusted frontends own connector obligations; deterministic workers
        # must not consume their attempts or mislabel them unsupported.
        task = self.store.claim(self.owner, self.lease_seconds, exclude_kinds=("operator_review",))
        if task is None:
            return None
        task_id = task["id"]
        stopped = threading.Event()
        lease_lost = threading.Event()

        def heartbeat():
            while not stopped.wait(max(0.01, self.lease_seconds / 3)):
                try:
                    self.store.renew(task_id, self.owner, self.lease_seconds)
                except Exception:
                    lease_lost.set()
                    return

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        commerce, execution_id, executed_at, capacity_recorded = None, None, None, False
        try:
            if task["checkpoint"] and task["checkpoint"].get("phase") == "workflow_finished":
                return self.store.complete(task_id, self.owner, task["checkpoint"]["result"])
            if task["kind"] != "operator_review":
                from .commercial import Commerce, CommercialError
                commerce = Commerce(self.store)
                classification = execution_class(task)
                execution_id = f"worker:{task_id}:attempt-{task['attempts']}"
                if classification == "exploratory_research":
                    with self.store.connection() as db:
                        overrun = db.execute("SELECT 1 FROM commerce_execution WHERE classification='exploratory_research' AND status='overrun' LIMIT 1").fetchone()
                        pending = db.execute("SELECT 1 FROM tasks WHERE id!=? AND status IN ('queued','running','deferred','blocked') AND priority>=80 LIMIT 1", (task_id,)).fetchone()
                    if overrun:
                        raise CommercialError("research_overrun", "An unresolved measured R&D overrun blocks additional independent research")
                    if pending:
                        raise CommercialError("customer_work_pending", "Customer delivery and acquisition obligations remain pending")
                commerce.reserve_execution(execution_id, classification, RESEARCH_SECONDS if classification == "exploratory_research" else 1.0)
                executed_at = time.monotonic()
            from .workflows import run_workflow
            # A killed attempt may have partial files. Preserve them, then give
            # the next attempt an empty directory instead of overwriting evidence.
            output_dir = self.store.artifact_root / task_id / f"attempt-{task['attempts']}"
            output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(output_dir, 0o700)
            self.store.checkpoint(task_id, self.owner, {"phase": "workflow_started", "attempt": task["attempts"]})
            if task["kind"] == "model_instruction":
                from .executor import run_model_instruction
                result = run_model_instruction(self.store, task, output_dir, inherited_fds=(slot_fd,))
            elif task["kind"] == "source_refresh":
                from .source_collection import collect_sources
                result = collect_sources(self.store, task["payload"], output_dir)
            elif task["kind"] == "research_sequence":
                result = _bounded_research(task["payload"], output_dir, slot_fd)
            else:
                result = run_workflow(task["kind"], task["payload"], output_dir)
            if isinstance(result, dict):
                result["artifact_directory"] = str(output_dir.relative_to(self.store.artifact_root))
            if executed_at is not None:
                capacity = commerce.complete_execution(execution_id, max(0, time.monotonic() - executed_at))
                capacity_recorded = True
                if isinstance(result, dict):
                    result["execution_capacity"] = {"id": execution_id, "classification": capacity["classification"], "actual_seconds": capacity["actual_units"], "status": capacity["status"]}
            if lease_lost.is_set():
                raise LeaseError("Lease lost during execution; artifacts retained for recovery")
            self.store.checkpoint(task_id, self.owner, {"phase": "workflow_finished", "result": result})
            return self.store.complete(task_id, self.owner, result)
        except LeaseError:
            return self.store.get_task(task_id)
        except Exception as error:
            code = getattr(error, "code", "")
            message = getattr(error, "message", str(error))
            # Unsupported kinds are a capability gap, not a transient exception.
            if code in {"unknown_kind", "unknown_workflow", "unsupported", "capability_unavailable", "unsupported_workflow", "research_capacity", "customer_work_pending", "research_overrun", "research_timeout"} or isinstance(error, NotImplementedError):
                return self.store.defer(task_id, self.owner, f"{code or 'unsupported'}: {message}")
            return self.store.fail(task_id, self.owner, f"{type(error).__name__}: {message}", retryable=not isinstance(error, (ValueError, FileNotFoundError, PermissionError)))
        finally:
            stopped.set()
            heartbeat_thread.join(timeout=2)
            if executed_at is not None and not capacity_recorded:
                commerce.complete_execution(execution_id, max(0, time.monotonic() - executed_at))

    def run(self, *, poll_interval: float = 2, max_tasks: int | None = None) -> int:
        completed = 0
        next_schedule = 0
        while not self.shutdown.is_set():
            if time.monotonic() >= next_schedule:
                try:
                    from .scheduler import tick
                    tick(self.store)
                except Exception as error:
                    with self.store.connection(write=True) as db:
                        self.store._event(db, "scheduler_failed", reason=type(error).__name__)
                next_schedule = time.monotonic() + 60
            result = self.step()
            if result is None:
                self.shutdown.wait(poll_interval)
            else:
                completed += 1
                if max_tasks is not None and completed >= max_tasks:
                    break
        return completed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=2)
    args = parser.parse_args(argv)
    if args.poll_interval <= 0:
        parser.error("poll interval must be positive")
    worker = Worker(Store(args.state_dir))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: worker.shutdown.set())
    if args.once:
        worker.step()
    else:
        worker.run(poll_interval=args.poll_interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
