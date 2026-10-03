from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from zoneinfo import ZoneInfo

from repvblicvs_engine.daemon import Worker
from repvblicvs_engine.scheduler import tick, claim_operator, complete_operator, defer_operator, renew_operator
from repvblicvs_engine.mcp import Server
from repvblicvs_engine.service import WakeController, wake_required
from repvblicvs_engine.source_collection import collect_sources, fetch, SourceCollectionError
from repvblicvs_engine.store import Store, ConflictError, LeaseError
from repvblicvs_engine.workflows import WorkflowError


def issue_fixture(count=20):
    return json.dumps({"items": [{"title": f"Synthetic request {number}", "html_url": f"https://github.com/example/project/issues/{number}", "body": "Synthetic source excerpt; no real customer", "updated_at": "2026-10-03T12:00:00Z"} for number in range(count)]}).encode()


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.now = datetime(2026, 10, 3, 7, tzinfo=ZoneInfo("America/New_York"))

    def test_daily_deduplication_across_frontends_and_concurrent_schedulers(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: tick(self.store, self.now), range(8)))
        tick(Store(self.directory.name), self.now)
        tasks = self.store.list_tasks()
        self.assertEqual(sum(task["kind"] == "source_refresh" for task in tasks), 1)
        self.assertEqual(sum(task["kind"] == "operator_review" for task in tasks), 1)
        self.assertEqual(next(task for task in tasks if task["kind"] == "source_refresh")["priority"], 80)

    def test_next_day_keeps_only_one_pending_operator_obligation(self):
        first = tick(self.store, self.now)
        tomorrow = datetime(2026, 10, 4, 7, tzinfo=ZoneInfo("America/New_York"))
        second = tick(self.store, tomorrow)
        self.assertNotEqual(first["source_task_id"], second["source_task_id"])
        self.assertEqual(first["operator_task_id"], second["operator_task_id"])
        self.assertEqual(sum(task["kind"] == "operator_review" for task in self.store.list_tasks()), 1)

    def test_control_and_six_am_gate(self):
        early = datetime(2026, 10, 3, 5, 59, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(tick(self.store, early)["scheduled"])
        self.store.pause()
        self.assertFalse(tick(self.store, self.now)["scheduled"])
        self.store.stop()
        self.assertFalse(tick(self.store, self.now)["scheduled"])
        self.assertEqual(self.store.list_tasks(), [])

    def test_allowlist_and_one_megabyte_cap(self):
        with patch("repvblicvs_engine.source_collection.request.build_opener") as opener:
            with self.assertRaises(WorkflowError):
                fetch("https://untrusted.example/input")
            opener.assert_not_called()
            response = MagicMock()
            response.geturl.return_value = "https://api.github.com/repos/openai/codex/releases?per_page=5"
            response.headers = {"Content-Length": "1000001"}
            opener.return_value.open.return_value.__enter__.return_value = response
            with self.assertRaises(SourceCollectionError):
                fetch("codex_releases")
        output = Path(self.directory.name) / "invalid"
        for payload in ({"urls": ["https://unknown.example"]}, {"feeds": ["unknown"]}, {"feeds": ["codex_releases", "codex_releases"]}):
            with self.assertRaises(WorkflowError):
                collect_sources(self.store, payload, output)

    def test_source_requests_stay_unqualified_bounded_and_inert(self):
        output = Path(self.directory.name) / "sources"
        with patch("repvblicvs_engine.source_collection.fetch", return_value=(issue_fixture(), {"http_status": 200})), patch("repvblicvs_engine.executor._run") as model:
            result = collect_sources(self.store, {"feeds": ["github_work_requests"]}, output)
        model.assert_not_called()
        self.assertEqual(result["summary"]["candidate_requests"], 10)
        self.assertEqual(result["summary"]["qualified_opportunities"], 0)
        candidates = json.loads((output / "candidates.json").read_text())["candidates"]
        self.assertTrue(all(not candidate["qualified"] and candidate["ai_permission"] == "unknown" and not candidate["solicited"] for candidate in candidates))
        self.assertEqual(result["summary"]["outreach_sent"], 0)

    def test_cached_stale_data_does_not_become_fresh(self):
        first = Path(self.directory.name) / "first"
        second = Path(self.directory.name) / "second"
        with patch("repvblicvs_engine.source_collection.fetch", return_value=(issue_fixture(2), {"http_status": 200})):
            collect_sources(self.store, {"feeds": ["github_work_requests"]}, first)
        with patch("repvblicvs_engine.source_collection.fetch", side_effect=URLError("fixture offline")):
            result = collect_sources(self.store, {"feeds": ["github_work_requests"]}, second)
        self.assertEqual(result["summary"]["fresh_sources"], 0)
        self.assertEqual(result["evidence"][0]["sources"][0]["status"], "cached_stale")
        self.assertIn("cached_observed_at", result["evidence"][0]["sources"][0])

    def test_network_failure_backoff_and_recovery_preserve_attempt_evidence(self):
        task = self.store.enqueue({"kind": "source_refresh", "payload": {"feeds": ["github_work_requests"]}, "max_attempts": 2})
        with patch("repvblicvs_engine.source_collection.fetch", side_effect=URLError("fixture offline")):
            failure = Worker(self.store).step()
        self.assertEqual(failure["status"], "queued")
        self.assertGreater(failure["next_run"], time.time())
        original = self.store.artifact_root / task["id"] / "attempt-1" / "sources.json"
        self.assertTrue(original.exists())
        with self.store.connection(write=True) as db:
            db.execute("UPDATE tasks SET next_run=0 WHERE id=?", (task["id"],))
        with patch("repvblicvs_engine.source_collection.fetch", return_value=(issue_fixture(1), {"http_status": 200})):
            recovered = Worker(self.store).step()
        self.assertEqual(recovered["status"], "completed")
        self.assertTrue(original.exists())
        self.assertIn("attempt-2", recovered["result"]["artifact_directory"])

    def test_source_refresh_runs_with_no_available_model_provider(self):
        task = self.store.enqueue({"kind": "source_refresh", "payload": {"feeds": ["codex_releases"]}})
        with patch("repvblicvs_engine.source_collection.fetch", return_value=(b"[]", {"http_status": 200})), patch("repvblicvs_engine.executor._run") as model:
            result = Worker(self.store).step()
        model.assert_not_called()
        self.assertEqual(result["id"], task["id"])
        self.assertEqual(result["status"], "completed")

    def test_wake_process_tracks_control_and_active_job_drain(self):
        fake = MagicMock()
        fake.poll.return_value = None
        controller = WakeController(self.store, 4321)
        with patch("repvblicvs_engine.service.sys.platform", "darwin"), patch("repvblicvs_engine.service.Path.exists", return_value=True), patch("repvblicvs_engine.service.subprocess.Popen", return_value=fake) as popen:
            controller.update()
            self.assertEqual(popen.call_args.args[0], ["/usr/bin/caffeinate", "-s", "-w", "4321"])
            task = self.store.enqueue({"kind": "test"})
            self.store.claim("worker")
            self.store.stop()
            self.assertTrue(wake_required(self.store))
            controller.update()
            fake.terminate.assert_not_called()
            self.store.complete(task["id"], "worker", {})
            self.assertFalse(wake_required(self.store))
            controller.update()
            fake.terminate.assert_called_once()
            self.assertIsNone(controller.process)
        self.store.pause()
        self.assertFalse(wake_required(self.store))

    def test_operator_claim_complete_shared_state_and_hashed_idempotent_receipt(self):
        scheduled = tick(self.store, self.now)
        claim = claim_operator(self.store, "codex-heartbeat", "turn-fixture-1", scheduled["operator_task_id"])
        second = claim_operator(Store(self.directory.name), "codex-heartbeat", "turn-fixture-1", scheduled["operator_task_id"])
        self.assertEqual(claim["lease_token"], second["lease_token"])
        renewed = renew_operator(self.store, "codex-heartbeat", "turn-fixture-1", claim["lease_token"])
        self.assertGreater(renewed["lease_expires"], time.time())
        evidence = {"source_refs": ["synthetic-source:fixture"], "summary": "Fixture reviewed; no real customer contacted"}
        result = {"qualified_opportunities": 0, "outreach_sent": 0}
        completed = complete_operator(Store(self.directory.name), "codex-heartbeat", "turn-fixture-1", claim["lease_token"], evidence, result)
        repeated = complete_operator(self.store, "codex-heartbeat", "turn-fixture-1", claim["lease_token"], evidence, result)
        self.assertEqual(completed, repeated)
        self.assertEqual(len(completed["content_hash"]), 64)
        self.assertEqual(self.store.get_task(scheduled["operator_task_id"])["status"], "completed")
        with self.assertRaises(ConflictError):
            complete_operator(self.store, "codex-heartbeat", "turn-fixture-1", claim["lease_token"], evidence, {"outreach_sent": 1})

    def test_operator_bridge_restricts_task_kind_identity_token_and_evidence(self):
        source = self.store.enqueue({"kind": "csv_cleanup", "payload": {"csv_text": "x\n1\n"}})
        with self.assertRaises(ValueError):
            claim_operator(self.store, "operator", "receipt-wrong-kind", source["id"])
        scheduled = tick(self.store, self.now)
        claim = claim_operator(self.store, "operator", "receipt-1", scheduled["operator_task_id"])
        with self.assertRaises(LeaseError):
            complete_operator(self.store, "wrong-operator", "receipt-1", claim["lease_token"], {"source_refs": ["fixture"]}, {})
        with self.assertRaises(LeaseError):
            complete_operator(self.store, "operator", "receipt-1", "wrong-token", {"source_refs": ["fixture"]}, {})
        with self.assertRaises(ValueError):
            complete_operator(self.store, "operator", "receipt-1", claim["lease_token"], {"summary": "unsupported claim"}, {})
        action = self.store.prepare_action("fixture-action", "delivery", {"synthetic": True})
        with self.assertRaises(ValueError):
            complete_operator(self.store, "operator", "receipt-1", claim["lease_token"], {"outbox_receipts": [action["id"]]}, {})
        self.store.complete_action(action["id"], "confirmed-fixture-receipt")
        completed = complete_operator(self.store, "operator", "receipt-1", claim["lease_token"], {"outbox_receipts": [action["id"]]}, {"synthetic": True})
        self.assertEqual(completed["status"], "completed")

    def test_operator_defer_and_expired_lease_preserve_obligation_and_audit(self):
        scheduled = tick(self.store, self.now)
        first = claim_operator(self.store, "operator", "one", scheduled["operator_task_id"])
        defer_operator(self.store, "operator", "one", first["lease_token"], "Fixture connector unavailable")
        self.assertEqual(self.store.get_task(scheduled["operator_task_id"])["status"], "deferred")
        second = claim_operator(self.store, "operator", "two", scheduled["operator_task_id"])
        with self.store.connection(write=True) as db:
            db.execute("UPDATE tasks SET lease_expires=0 WHERE id=?", (scheduled["operator_task_id"],))
        self.store.recover()
        self.assertEqual(self.store.get_task(scheduled["operator_task_id"])["status"], "deferred")
        with self.assertRaises((LeaseError, ConflictError)):
            renew_operator(self.store, "operator", "two", second["lease_token"])
        third = claim_operator(self.store, "operator", "three", scheduled["operator_task_id"])
        self.assertEqual(third["task"]["attempts"], 3)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts WHERE task_id=?", (scheduled["operator_task_id"],)).fetchone()[0], 3)

    def test_mcp_frontends_consume_same_operator_obligation(self):
        scheduled = tick(self.store, self.now)
        codex, claude = Server(self.store), Server(Store(self.directory.name))
        claim = codex.call_tool("engine_operator_claim", {"operator_id": "codex-heartbeat", "receipt_id": "mcp-fixture", "task_id": scheduled["operator_task_id"]})
        inspected = claude.call_tool("engine_tasks", {"task_id": scheduled["operator_task_id"]})
        self.assertEqual(inspected["status"], "running")
        complete = claude.call_tool("engine_operator_complete", {"operator_id": "codex-heartbeat", "receipt_id": "mcp-fixture", "lease_token": claim["lease_token"], "evidence": {"source_refs": ["synthetic-review-fixture"]}, "result": {"synthetic": True}})
        self.assertEqual(complete["status"], "completed")


if __name__ == "__main__":
    unittest.main()
