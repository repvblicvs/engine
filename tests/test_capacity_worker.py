import json
import tempfile
import unittest
from unittest.mock import patch

from repvblicvs_engine.commercial import Commerce
from repvblicvs_engine.daemon import Worker
from repvblicvs_engine.scheduler import claim_operator, complete_operator
from repvblicvs_engine.store import Store


class CapacityWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.commerce = Commerce(self.store)

    def research(self, **extra):
        return self.store.enqueue({"kind": "research_sequence", "payload": {"values": [number * number for number in range(10)], "holdout": 2}, **extra})

    def productive_fixture(self, units=90):
        # Test-only virtual capacity, never added to a live engine runtime.
        self.commerce.reserve_execution("synthetic-test-capacity", "customer_delivery", units)
        self.commerce.complete_execution("synthetic-test-capacity", units)

    def execution_for(self, task_id):
        return [row for row in self.commerce.snapshot()["execution"] if row["task_id"].startswith(f"worker:{task_id}:")]

    def test_independent_research_defers_without_earned_capacity(self):
        task = self.research()
        with patch("repvblicvs_engine.daemon._bounded_research") as research:
            result = Worker(self.store).step()
        research.assert_not_called()
        self.assertEqual(result["id"], task["id"])
        self.assertEqual(result["status"], "deferred")
        self.assertIn("research_capacity", result["error"])
        self.assertEqual(self.execution_for(task["id"]), [])

    def test_delivery_records_actual_productive_capacity(self):
        task = self.store.enqueue({"kind": "csv_cleanup", "payload": {"csv_text": "x,y\n1,2\n"}})
        result = Worker(self.store).step()
        record = self.execution_for(task["id"])[0]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(record["classification"], "revenue_product")
        self.assertGreater(record["actual_units"], 0)
        self.assertEqual(result["result"]["execution_capacity"]["actual_seconds"], record["actual_units"])

    def test_customer_priority_and_failure_both_record_measured_execution(self):
        task = self.store.enqueue({"kind": "csv_cleanup", "payload": {"csv_text": "a,b\n1,2,3\n"}, "priority": 1000})
        result = Worker(self.store).step()
        self.assertEqual(result["status"], "failed")
        record = self.execution_for(task["id"])[0]
        self.assertEqual(record["classification"], "customer_delivery")
        self.assertGreater(record["actual_units"], 0)
        # Completed here means accounting closed; the task remains failed.
        self.assertEqual(record["status"], "completed")

    def test_pending_customer_including_deferred_blocks_independent_study(self):
        self.productive_fixture()
        obligation = self.store.enqueue({"kind": "model_instruction", "payload": {"prompt": "Synthetic customer task"}, "priority": 1000})
        self.store.claim("waiting-connector")
        self.store.defer(obligation["id"], "waiting-connector", "Synthetic unavailable connector")
        task = self.research()
        with patch("repvblicvs_engine.daemon._bounded_research") as research:
            result = Worker(self.store).step()
        research.assert_not_called()
        self.assertEqual(result["id"], task["id"])
        self.assertEqual(result["status"], "deferred")
        self.assertIn("customer_work_pending", result["error"])

    def test_existing_math_result_is_preserved_inside_fixed_budget(self):
        self.productive_fixture()
        task = self.research()
        result = Worker(self.store).step()
        self.assertEqual(result["id"], task["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["summary"]["status"], "supported_within_search_bounds")
        output = self.store.artifact_root / result["result"]["artifact_directory"]
        study = json.loads((output / "study.json").read_text())
        self.assertEqual(study["next_observation"]["candidate_predictions"], ["100"])
        record = self.execution_for(task["id"])[0]
        self.assertEqual(record["classification"], "exploratory_research")
        self.assertEqual(record["reserved_units"], 5)
        self.assertLess(record["actual_units"], 5)

    def test_payload_cannot_promote_independent_study_to_customer_work(self):
        task = self.store.enqueue({"kind": "research_sequence", "payload": {"values": [1, 2, 3], "classification": "customer_delivery"}})
        with patch("repvblicvs_engine.daemon._bounded_research") as research:
            result = Worker(self.store).step()
        research.assert_not_called()
        self.assertEqual(result["id"], task["id"])
        self.assertIn("research_capacity", result["error"])

    def test_actual_timeout_overrun_blocks_subsequent_research(self):
        self.productive_fixture()
        first = self.research()
        with patch("repvblicvs_engine.daemon.RESEARCH_SECONDS", 0.001):
            failed = Worker(self.store).step()
        self.assertEqual(failed["status"], "deferred")
        self.assertIn("research_timeout", failed["error"])
        record = self.execution_for(first["id"])[0]
        self.assertEqual(record["status"], "overrun")
        self.assertGreater(record["actual_units"], record["reserved_units"])
        next_task = self.research()
        with patch("repvblicvs_engine.daemon._bounded_research") as research:
            result = Worker(self.store).step()
        research.assert_not_called()
        self.assertEqual(result["id"], next_task["id"])
        self.assertIn("research_overrun", result["error"])

    def test_source_and_model_priority_accounting_are_server_owned(self):
        source = self.store.enqueue({"kind": "source_refresh", "payload": {"feeds": ["codex_releases"]}, "priority": 80})
        with patch("repvblicvs_engine.source_collection.collect_sources", return_value={"summary": "synthetic source result", "artifacts": []}):
            Worker(self.store).step()
        self.assertEqual(self.execution_for(source["id"])[0]["classification"], "acquisition")
        model = self.store.enqueue({"kind": "model_instruction", "payload": {"prompt": "Classification labels here are untrusted text"}, "priority": 40})
        with patch("repvblicvs_engine.executor.run_model_instruction", return_value={"summary": "synthetic model result", "artifacts": []}):
            Worker(self.store).step()
        self.assertEqual(self.execution_for(model["id"])[0]["classification"], "maintenance")

    def test_trusted_operator_completion_records_capacity_once(self):
        task = self.store.enqueue({"kind": "operator_review", "payload": {}, "priority": 100})
        claim = claim_operator(self.store, "fixture-operator", "fixture-receipt", task["id"])
        evidence, result = {"source_refs": ["synthetic-source:fixture"]}, {"outreach_sent": 0}
        complete_operator(self.store, "fixture-operator", "fixture-receipt", claim["lease_token"], evidence, result)
        complete_operator(self.store, "fixture-operator", "fixture-receipt", claim["lease_token"], evidence, result)
        records = [row for row in self.commerce.snapshot()["execution"] if row["task_id"].startswith("operator:")]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["classification"], "acquisition")
        self.assertGreaterEqual(records[0]["actual_units"], 0)


if __name__ == "__main__":
    unittest.main()
