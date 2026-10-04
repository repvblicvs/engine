from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from repvblicvs_engine.daemon import Worker
from repvblicvs_engine.providers import Router, ProviderUnavailable
from repvblicvs_engine.service import plist_document
from repvblicvs_engine.store import Store, ConflictError, LeaseError


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)

    def enqueue(self, **extra):
        return self.store.enqueue({"kind": "test", **extra})

    def expire(self, task_id):
        with self.store.connection(write=True) as db:
            db.execute("UPDATE tasks SET lease_expires=0 WHERE id=?", (task_id,))

    def test_persistent_request_deduplication_and_conflict(self):
        spec = {"kind": "test", "payload": {"value": 2}}
        original = self.store.enqueue(spec, "request-1")
        other_frontend = Store(self.directory.name)
        self.assertEqual(original["id"], other_frontend.enqueue(spec, "request-1")["id"])
        self.assertEqual(len(other_frontend.list_tasks()), 1)
        with self.assertRaises(ConflictError):
            other_frontend.enqueue({"kind": "test", "payload": {"value": 3}}, "request-1")

    def test_priority_dependencies_and_shared_controls(self):
        first = self.enqueue(priority=1)
        second = self.enqueue(priority=9, dependencies=[first["id"]])
        self.store.pause()
        self.assertIsNone(self.store.claim("one"))
        Store(self.directory.name).resume()
        claimed = self.store.claim("one")
        self.assertEqual(claimed["id"], first["id"])
        self.store.complete(first["id"], "one", {"ok": True})
        self.assertEqual(self.store.claim("two")["id"], second["id"])
        self.store.stop()
        self.assertIsNone(self.store.claim("three"))

    def test_failed_dependency_blocks_downstream(self):
        first = self.enqueue(max_attempts=1)
        child = self.enqueue(dependencies=[first["id"]])
        self.store.claim("worker")
        self.store.fail(first["id"], "worker", "invalid", retryable=False)
        self.assertIsNone(self.store.claim("worker"))
        self.assertEqual(self.store.get_task(child["id"])["status"], "blocked")

    def test_expired_lease_preserves_checkpoint_and_rejects_old_worker(self):
        task = self.enqueue()
        self.store.claim("old")
        self.store.checkpoint(task["id"], "old", {"offset": 42})
        self.expire(task["id"])
        recovered = Store(self.directory.name).claim("new")
        self.assertEqual(recovered["checkpoint"], {"offset": 42})
        self.assertEqual(recovered["attempts"], 2)
        with self.assertRaises(LeaseError):
            self.store.complete(task["id"], "old", {})
        self.store.complete(task["id"], "new", {"ok": True})
        self.assertEqual(self.store.get_task(task["id"])["status"], "completed")

    def test_final_crash_marks_failed(self):
        task = self.enqueue(max_attempts=1)
        self.store.claim("lost")
        self.expire(task["id"])
        self.store.recover()
        self.assertEqual(self.store.get_task(task["id"])["status"], "failed")

    def test_bounded_retry_and_backoff(self):
        task = self.enqueue(max_attempts=2)
        self.store.claim("w")
        failed = self.store.fail(task["id"], "w", "transient")
        self.assertEqual(failed["status"], "queued")
        self.assertGreater(failed["next_run"], time.time())
        self.assertIsNone(self.store.claim("w"))
        with self.store.connection(write=True) as db:
            db.execute("UPDATE tasks SET next_run=0 WHERE id=?", (task["id"],))
        self.store.claim("w")
        self.assertEqual(self.store.fail(task["id"], "w", "again")["status"], "failed")

    def test_two_worker_limit_and_no_duplicate_claims(self):
        for _ in range(8):
            self.enqueue()
        with ThreadPoolExecutor(max_workers=8) as executor:
            claims = list(executor.map(lambda index: self.store.claim(str(index)), range(8)))
        claimed = [task for task in claims if task]
        self.assertEqual(len(claimed), 2)
        self.assertEqual(len({task["id"] for task in claimed}), 2)

    def test_unknown_capability_deferred_not_retried(self):
        class UnsupportedError(Exception):
            code = "unknown_kind"
        def unsupported(*_):
            raise UnsupportedError("model route not installed")
        module = types.ModuleType("repvblicvs_engine.workflows")
        module.run_workflow = unsupported
        task = self.enqueue()
        with patch.dict(sys.modules, {"repvblicvs_engine.workflows": module}):
            result = Worker(self.store).step()
        self.assertEqual(result["status"], "deferred")
        self.assertIsNone(self.store.claim("again"))
        self.store.retry_deferred(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "queued")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts WHERE task_id=?", (task["id"],)).fetchone()[0], 1)
        self.assertEqual(self.store.claim("new")["attempts"], 2)

    def test_finished_checkpoint_completes_without_reexecution(self):
        task = self.enqueue()
        self.store.claim("crashed")
        self.store.checkpoint(task["id"], "crashed", {"phase": "workflow_finished", "result": {"delivered": True}})
        self.expire(task["id"])
        result = Worker(self.store).step()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"], {"delivered": True})

    def test_finished_checkpoint_survives_final_execution_attempt(self):
        for maximum in (1, 2):
            with self.subTest(max_attempts=maximum):
                task = self.enqueue(max_attempts=maximum)
                for attempt in range(maximum):
                    self.store.claim("crashed")
                    if attempt + 1 < maximum:
                        self.store.fail(task["id"], "crashed", "Synthetic transient failure")
                        with self.store.connection(write=True) as db:
                            db.execute("UPDATE tasks SET next_run=0 WHERE id=?", (task["id"],))
                saved_result = {"delivered": True, "execution_attempt": maximum}
                self.store.checkpoint(task["id"], "crashed", {"phase": "workflow_finished", "result": saved_result})
                self.expire(task["id"])
                self.store.recover()
                pending = self.store.get_task(task["id"])
                self.assertEqual(pending["status"], "queued")
                self.assertEqual(pending["retry_count"], maximum - 1)
                with patch("repvblicvs_engine.workflows.run_workflow") as workflow:
                    result = Worker(self.store).step()
                workflow.assert_not_called()
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["result"], saved_result)
                self.assertEqual(result["retry_count"], maximum - 1)
                with self.store.connection() as db:
                    outcomes = [row[0] for row in db.execute("SELECT outcome FROM attempts WHERE task_id=? ORDER BY number", (task["id"],))]
                self.assertEqual(outcomes[-2:], ["lease_expired", "completed"])

    def test_incomplete_finished_checkpoint_does_not_bypass_retry_ceiling(self):
        task = self.enqueue(max_attempts=1)
        self.store.claim("crashed")
        self.store.checkpoint(task["id"], "crashed", {"phase": "workflow_finished"})
        self.expire(task["id"])
        self.store.recover()
        self.assertEqual(self.store.get_task(task["id"])["status"], "failed")

    def test_worker_reclaims_use_distinct_owners_and_reject_stale_mutations(self):
        task = self.enqueue()
        worker = Worker(self.store)
        owners = []

        def workflow(*_):
            owners.append(self.store.get_task(task["id"])["lease_owner"])
            if len(owners) == 1:
                self.expire(task["id"])
                raise LeaseError("Synthetic lease loss")
            stale = owners[0]
            mutations = (
                lambda: self.store.renew(task["id"], stale),
                lambda: self.store.checkpoint(task["id"], stale, {"stale": True}),
                lambda: self.store.complete(task["id"], stale, {"stale": True}),
                lambda: self.store.fail(task["id"], stale, "Stale failure"),
                lambda: self.store.defer(task["id"], stale, "Stale capability gap"),
            )
            for mutation in mutations:
                with self.assertRaises(LeaseError):
                    mutation()
            return {"synthetic": True}

        with patch("repvblicvs_engine.workflows.run_workflow", side_effect=workflow):
            self.assertEqual(worker.step()["status"], "running")
            result = worker.step()
        self.assertNotEqual(owners[0], owners[1])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], 2)
        self.assertTrue(result["result"]["synthetic"])

    def test_run_survives_lease_loss_during_failure_and_defer(self):
        class UnsupportedError(Exception):
            code = "capability_unavailable"

        for failure in (RuntimeError, UnsupportedError):
            with self.subTest(failure=failure.__name__), tempfile.TemporaryDirectory() as directory:
                store = Store(directory)
                first = store.enqueue({"kind": "test", "priority": 1})
                second = store.enqueue({"kind": "test"})
                calls = []

                def workflow(*_):
                    calls.append(True)
                    if len(calls) == 1:
                        with store.connection(write=True) as db:
                            db.execute("UPDATE tasks SET lease_expires=0 WHERE id=?", (first["id"],))
                        replacement = store.claim("replacement-worker")
                        self.assertEqual(replacement["id"], first["id"])
                        store.checkpoint(first["id"], "replacement-worker", {"replacement": True})
                        raise failure("Synthetic workflow error after lease loss")
                    return {"synthetic": True}

                with patch("repvblicvs_engine.workflows.run_workflow", side_effect=workflow), patch("repvblicvs_engine.scheduler.tick"):
                    self.assertEqual(Worker(store).run(max_tasks=2), 2)
                current = store.get_task(first["id"])
                self.assertEqual(current["status"], "running")
                self.assertEqual(current["lease_owner"], "replacement-worker")
                self.assertEqual(current["attempts"], 2)
                self.assertEqual(current["retry_count"], 1)
                self.assertEqual(current["checkpoint"], {"replacement": True})
                self.assertEqual(store.get_task(second["id"])["status"], "completed")

    def test_real_csv_workflow_from_queue_and_partial_attempt_preserved(self):
        task = self.store.enqueue({"kind": "csv_cleanup", "payload": {"csv_text": "Item, Value\na, 2\nb, 3\n"}})
        self.store.claim("crashed")
        partial = self.store.artifact_root / task["id"] / "attempt-1"
        partial.mkdir(parents=True)
        (partial / "partial.txt").write_text("interrupted evidence")
        self.expire(task["id"])
        result = Worker(self.store).step()
        self.assertEqual(result["status"], "completed")
        self.assertTrue((partial / "partial.txt").exists())
        artifact_dir = self.store.artifact_root / result["result"]["artifact_directory"]
        self.assertEqual((artifact_dir / "cleaned.csv").read_text(), "item,value\na,2\nb,3\n")
        self.assertIn("attempt-2", result["result"]["artifact_directory"])

    def test_write_leases_serialize_parent_and_child_ownership(self):
        source = Path(self.directory.name) / "source"
        self.store.acquire_write_leases("worker-a", [str(source)])
        with self.assertRaises(LeaseError):
            self.store.acquire_write_leases("worker-b", [str(source / "module.py")])
        self.store.acquire_write_leases("worker-a", [str(source / "module.py")])
        self.store.release_write_leases("worker-a")
        self.store.acquire_write_leases("worker-b", [str(source / "module.py")])
        with self.assertRaises(LeaseError):
            self.store.acquire_write_leases("worker-a", [str(source)])

    def test_outbox_idempotency_and_ambiguous_reconciliation(self):
        action = self.store.prepare_action("delivery-1", "delivery", {"file": "report.csv"})
        self.assertEqual(action["id"], self.store.prepare_action("delivery-1", "delivery", {"file": "report.csv"})["id"])
        self.store.complete_action(action["id"], uncertain=True)
        self.assertEqual(self.store.list_actions()[0]["status"], "unknown")
        self.store.complete_action(action["id"], "receipt-1")
        self.store.complete_action(action["id"], "receipt-1")
        with self.assertRaises(ConflictError):
            self.store.complete_action(action["id"], "receipt-2")
        self.assertEqual(len(self.store.list_actions()), 1)

    def test_dispatching_action_crash_requires_reconciliation(self):
        action = self.store.prepare_action("send-once", "email", {"synthetic": True})
        self.assertEqual(self.store.begin_action(action["id"])["status"], "dispatching")
        Store(self.directory.name).reconcile_actions()
        self.assertEqual(self.store.list_actions()[0]["status"], "unknown")
        with self.assertRaises(ConflictError):
            self.store.begin_action(action["id"])
        self.store.complete_action(action["id"], "verified-receipt")
        self.assertEqual(self.store.list_actions()[0]["status"], "delivered")

    def test_runtime_files_are_private_from_creation(self):
        self.enqueue()
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)
        with self.store.connection() as db:
            db.execute("SELECT * FROM tasks").fetchall()
            for suffix in ("-wal", "-shm"):
                path = Path(str(self.store.path) + suffix)
                if path.exists():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_costs_are_idempotent(self):
        self.store.record_cost("fable", "claude", 1.5, "cost-1", included=False)
        self.store.record_cost("fable", "claude", 1.5, "cost-1", included=False)
        self.assertEqual(self.store.status()["recorded_paid_cost"], 1.5)
        with self.assertRaises(ConflictError):
            self.store.record_cost("fable", "claude", 2.0, "cost-1", included=False)

    def test_invalid_specs_are_not_accepted(self):
        for spec in ({}, {"kind": "test", "command": "rm"}, {"kind": "test", "payload": []}, {"kind": "test", "max_attempts": 100}, {"kind": "test", "dependencies": ["missing"]}, {"kind": "test", "priority": True}):
            with self.assertRaises(ValueError):
                self.store.enqueue(spec)

    def configure_routes(self, calls=10):
        router = Router(self.store)
        until = time.time() + 3600
        router.configure_account("claude", evidence={"source": "test included allowance"}, verified_until=until, remaining_calls=calls, available_credit=5)
        router.configure_account("openai", evidence={"source": "test included allowance"}, verified_until=until, remaining_calls=10)
        router.configure_provider("fable", "claude", evidence={"source": "test model entitlement"}, verified_until=until, status="ready", budget=5, included_only=False)
        router.configure_provider("opus", "claude", evidence={"source": "test model entitlement"}, verified_until=until, status="ready")
        router.configure_provider("sol", "openai", evidence={"source": "test model entitlement"}, verified_until=until, status="ready")
        return router

    def test_fable_five_dollar_cap_and_fallback_persist(self):
        router = self.configure_routes()
        for index in range(2):
            call = router.reserve(f"call-{index}", ceiling=2.5)
            self.assertEqual(call["provider"], "fable")
            router.settle(f"call-{index}", 2.5)
        fallback = Router(Store(self.directory.name)).reserve("call-3", ceiling=2.5)
        self.assertEqual(fallback["provider"], "opus")
        self.assertEqual(fallback["ceiling"], 0)
        with self.assertRaises(ValueError):
            router.configure_provider("fable", "claude", budget=5.01)

    def test_shared_account_quota_falls_back_across_labels(self):
        router = self.configure_routes(calls=1)
        self.assertEqual(router.reserve("one", ceiling=1)["provider"], "fable")
        self.assertEqual(router.reserve("two", preferred="opus")["provider"], "sol")

    def test_default_routes_are_unavailable_without_live_evidence(self):
        router = Router(self.store)
        self.assertTrue(all(not item["currently_verified"] for item in router.list()))
        with self.assertRaises(ProviderUnavailable):
            router.reserve("unsafe")
        with self.assertRaises(ValueError):
            router.configure_account("test", evidence={}, verified_until=time.time() + 100)

    def test_stale_provider_evidence_cannot_run(self):
        router = self.configure_routes()
        with self.store.connection(write=True) as db:
            db.execute("UPDATE accounts SET verified_until=0")
        with self.assertRaises(ProviderUnavailable):
            router.reserve("stale", ceiling=1)

    def test_reservation_deduplication_and_no_call_release(self):
        router = self.configure_routes()
        first = router.reserve("once", ceiling=2)
        second = router.reserve("once", ceiling=2)
        self.assertEqual(first["provider"], second["provider"])
        with self.assertRaises(ConflictError):
            router.reserve("once", ceiling=3)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT available_credit FROM accounts WHERE id='claude'").fetchone()[0], 3)
        router.release("once", evidence_no_call="Process creation failed before invocation")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT available_credit FROM accounts WHERE id='claude'").fetchone()[0], 5)

    def test_service_definition_is_canonical_and_persistent(self):
        doc = plist_document(sys.executable, self.directory.name)
        self.assertTrue(doc["KeepAlive"])
        self.assertTrue(doc["RunAtLoad"])
        self.assertIn("repvblicvs_engine.service", doc["ProgramArguments"])
        self.assertEqual(doc["EnvironmentVariables"]["REPVBLICVS_STATE_DIR"], str(Path(self.directory.name).resolve()))


if __name__ == "__main__":
    unittest.main()
