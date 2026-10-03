import json
import fcntl
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from repvblicvs_engine.executor import ProcessNotStarted, _run, classify_failure, command, run_model_instruction
from repvblicvs_engine.providers import Router
from repvblicvs_engine.store import Store
from repvblicvs_engine.workflows import WorkflowError


def opus_result(answer="REVIEW_READY"):
    return {"exit_code": 0, "stdout": json.dumps({"result": answer, "is_error": False, "total_cost_usd": 0.02, "usage": {"output_tokens": 8}}), "stderr": "", "failure": None}


def sol_result(answer="FALLBACK_READY"):
    return {"exit_code": 0, "stdout": "\n".join(json.dumps(event) for event in ({"type": "item.completed", "item": {"type": "agent_message", "text": answer}}, {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}})), "stderr": "", "failure": None}


def credit_limits(balance="120"):
    now = time.time()
    return {"ordinaryUsageAllowed": False, "rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": now + 1000}, "secondary": {"usedPercent": 36, "windowDurationMins": 10080, "resetsAt": now + 10000}, "credits": {"hasCredits": True, "balance": balance}, "spendControlReached": False, "rateLimitReachedType": "rate_limit_reached"}}}


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.router = Router(self.store)
        self.until = time.time() + 3600
        for name in ("claude", "openai", "github"):
            self.router.configure_account(name, evidence={"source": "test included allowance"}, verified_until=self.until, remaining_calls=6)
        self.router.configure_provider("opus", "claude", model="claude-opus-5-5", status="ready", verified_until=self.until, evidence={"paid_usage_disabled": True, "auto_reload_disabled": True, "auth": "oauth"})
        self.router.configure_provider("sol", "openai", model="gpt-6.1-sol", status="ready", verified_until=self.until, evidence={"auth": "chatgpt", "limit_id": "codex"})
        self.task = self.store.enqueue({"kind": "model_instruction", "payload": {"prompt": "Review the synthetic design.", "requirements": ["Return a concise answer"]}})
        self.output = self.store.artifact_root / self.task["id"] / "attempt-1"
        self.output.mkdir(parents=True)

    def authorize_existing_credits(self):
        policy = {"authorized": True, "scope": "existing_codex_balance", "initial_balance": 120, "reserve_credits": 50, "max_balance_to_use": 70, "authorization_receipt": "synthetic-owner-direct-receipt", "purchases_allowed": False, "refill_allowed": False}
        with self.store.connection(write=True) as db:
            db.execute("INSERT INTO meta VALUES('codex_credit_policy',?)", (json.dumps(policy),))
        self.router.unavailable("opus", "not available")
        self.task["payload"]["preferred"] = "sol"

    def test_opus_answer_is_untrusted_artifact_and_completed_receipt_reused(self):
        with patch("repvblicvs_engine.executor._run", return_value=opus_result()) as run, patch("repvblicvs_engine.executor.shutil.which", return_value="/fake/claude"):
            result = run_model_instruction(self.store, self.task, self.output)
            recovered = run_model_instruction(self.store, self.task, self.output)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["summary"]["provider"], "opus")
        self.assertEqual(recovered["summary"]["provider"], "opus")
        self.assertEqual((self.output / "model-response.md").read_text(), "REVIEW_READY")
        self.assertFalse(result["evidence"][0]["generated_code_executed"])
        self.assertEqual(self.store.status()["recorded_paid_cost"], 0)

    def test_explicit_quota_refusal_falls_back_with_same_packet(self):
        failure = {"exit_code": 1, "stdout": "", "stderr": "Usage limit reached", "failure": None}
        with patch("repvblicvs_engine.executor._run", side_effect=[failure, sol_result()]) as run, patch("repvblicvs_engine.executor.refresh_sol", return_value={"dispatch": {"allowed": True, "billing_mode": "included"}}) as refresh, patch("repvblicvs_engine.executor.command", side_effect=lambda *args: [args[0]]):
            result = run_model_instruction(self.store, self.task, self.output)
        self.assertEqual(result["summary"]["provider"], "sol")
        self.assertEqual(run.call_args_list[0].args[1], run.call_args_list[1].args[1])
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(next(provider for provider in self.router.list() if provider["name"] == "opus")["account_status"], "exhausted")

    def test_ambiguous_timeout_is_not_replayed_or_fallen_back(self):
        failure = {"exit_code": -9, "stdout": "partial", "stderr": "", "failure": "timeout"}
        with patch("repvblicvs_engine.executor._run", return_value=failure) as run, patch("repvblicvs_engine.executor.command", return_value=["fake"]):
            for _ in range(2):
                with self.assertRaises(WorkflowError):
                    run_model_instruction(self.store, self.task, self.output)
        self.assertEqual(run.call_count, 1)
        receipt = json.loads(next((self.store.root / "model_calls").glob("*.json")).read_text())
        self.assertEqual(receipt["status"], "unknown")

    def test_unattested_paid_gate_never_starts_opus(self):
        self.router.configure_provider("opus", "claude", model="claude-opus-5-5", status="ready", verified_until=self.until, evidence={"auth": "oauth"})
        with patch("repvblicvs_engine.executor.refresh_sol", side_effect=RuntimeError("No allowance")), patch("repvblicvs_engine.executor._run") as run:
            with self.assertRaises(WorkflowError):
                run_model_instruction(self.store, self.task, self.output)
        run.assert_not_called()

    def test_expired_sol_account_gets_fresh_preflight(self):
        self.router.unavailable("opus", "not available")
        with self.store.connection(write=True) as db:
            db.execute("UPDATE accounts SET verified_until=0 WHERE id='openai'")
        def refresh(store, router, provider):
            router.configure_account("openai", evidence={"source": "fresh test"}, verified_until=time.time() + 30, remaining_calls=1)
            return {"dispatch": {"allowed": True, "billing_mode": "included"}}
        with patch("repvblicvs_engine.executor.refresh_sol", side_effect=refresh) as preflight, patch("repvblicvs_engine.executor._run", return_value=sol_result()), patch("repvblicvs_engine.executor.command", return_value=["fake"]):
            result = run_model_instruction(self.store, self.task, self.output)
        self.assertEqual(result["summary"]["provider"], "sol")
        self.assertEqual(preflight.call_count, 2)

    def test_prepaid_credit_call_is_bounded_and_accounted_separately_from_dollars(self):
        self.authorize_existing_credits()
        with patch("repvblicvs_engine.allowance.read_live_limits", side_effect=[credit_limits("120"), credit_limits("117.5")]) as read, patch("repvblicvs_engine.executor._run", return_value=sol_result()) as run, patch("repvblicvs_engine.executor.command", return_value=["fake"]):
            result = run_model_instruction(self.store, self.task, self.output)
            recovered = run_model_instruction(self.store, self.task, self.output)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(read.call_count, 2)  # non-inference preflight + postflight
        self.assertEqual(run.call_args.args[3:5], (90, 65536))
        evidence = result["evidence"][0]
        self.assertEqual(evidence["billing_mode"], "authorized_existing_credits")
        self.assertFalse(evidence["included_allowance"])
        self.assertEqual(evidence["usd_cash_charge_recorded"], 0)
        self.assertEqual(evidence["observed_shared_account_depletion_since_preflight_credits"], 2.5)
        self.assertIn("unattributed", evidence["credit_depletion_attribution"])
        self.assertEqual(recovered["evidence"][0]["billing_mode"], "authorized_existing_credits")
        with self.store.connection() as db:
            cost = db.execute("SELECT * FROM costs WHERE provider='sol'").fetchone()
            self.assertEqual(cost["amount"], 0)
            self.assertEqual(cost["included"], 0)

    def test_existing_balance_without_owner_policy_never_launches_model(self):
        self.router.unavailable("opus", "not available")
        with patch("repvblicvs_engine.allowance.read_live_limits", return_value=credit_limits()), patch("repvblicvs_engine.executor._run") as run:
            with self.assertRaises(WorkflowError):
                run_model_instruction(self.store, self.task, self.output)
        run.assert_not_called()

    def test_explicit_oversized_credit_requirements_are_preserved_and_deferred(self):
        for extra in ({"timeout_seconds": 91}, {"max_output_bytes": 65537}, {"prompt": "x" * 19900}):
            with self.subTest(extra=next(iter(extra))):
                self.authorize_existing_credits() if not self.task["payload"].get("preferred") else None
                self.task["payload"] = {"prompt": "Review synthetic evidence", "preferred": "sol", **extra}
                before = dict(self.task["payload"])
                with patch("repvblicvs_engine.allowance.read_live_limits", return_value=credit_limits()), patch("repvblicvs_engine.executor._run") as run:
                    with self.assertRaisesRegex(WorkflowError, "owner review required"):
                        run_model_instruction(self.store, self.task, self.output)
                run.assert_not_called()
                self.assertEqual(self.task["payload"], before)

    def test_credit_postflight_failure_retains_completed_receipt_without_replay(self):
        self.authorize_existing_credits()
        with patch("repvblicvs_engine.allowance.read_live_limits", side_effect=[credit_limits(), RuntimeError("Offline after completion")]) as read, patch("repvblicvs_engine.executor._run", return_value=sol_result()) as run, patch("repvblicvs_engine.executor.command", return_value=["fake"]):
            result = run_model_instruction(self.store, self.task, self.output)
            recovered = run_model_instruction(self.store, self.task, self.output)
        self.assertEqual(result["evidence"][0]["postflight_observation_status"], "unavailable")
        self.assertEqual(recovered["summary"]["provider"], "sol")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(read.call_count, 2)

    def test_completed_credit_receipt_survives_expired_route_and_new_fallback(self):
        self.authorize_existing_credits()
        with patch("repvblicvs_engine.allowance.read_live_limits", side_effect=[credit_limits(), credit_limits("119")]), patch("repvblicvs_engine.executor._run", return_value=sol_result()), patch("repvblicvs_engine.executor.command", return_value=["fake"]):
            first = run_model_instruction(self.store, self.task, self.output)
        self.router.unavailable("sol", "entitlement expired")
        self.router.configure_provider("opus", "claude", model="claude-opus-5-5", status="ready", verified_until=self.until, evidence={"paid_usage_disabled": True, "auto_reload_disabled": True, "auth": "oauth"})
        self.task["payload"]["preferred"] = "opus"  # routing choice is outside prompt digest
        with patch("repvblicvs_engine.allowance.read_live_limits") as read, patch("repvblicvs_engine.executor._run") as run:
            recovered = run_model_instruction(self.store, self.task, self.output)
        self.assertEqual(recovered["summary"]["provider"], first["summary"]["provider"])
        read.assert_not_called()
        run.assert_not_called()

    def test_ambiguous_credit_receipt_blocks_even_after_route_becomes_unavailable(self):
        self.authorize_existing_credits()
        failure = {"exit_code": -9, "stdout": "partial", "stderr": "", "failure": "timeout"}
        with patch("repvblicvs_engine.allowance.read_live_limits", side_effect=[credit_limits(), credit_limits("119")]), patch("repvblicvs_engine.executor._run", return_value=failure), patch("repvblicvs_engine.executor.command", return_value=["fake"]):
            with self.assertRaises(WorkflowError):
                run_model_instruction(self.store, self.task, self.output)
        self.router.unavailable("sol", "entitlement expired")
        self.router.configure_provider("opus", "claude", model="claude-opus-5-5", status="ready", verified_until=self.until, evidence={"paid_usage_disabled": True, "auto_reload_disabled": True, "auth": "oauth"})
        with patch("repvblicvs_engine.executor._run") as run:
            with self.assertRaisesRegex(WorkflowError, "ambiguous outcome"):
                run_model_instruction(self.store, self.task, self.output)
        run.assert_not_called()

    def test_missing_preflight_mode_and_unattested_oauth_do_not_launch_sol(self):
        self.router.unavailable("opus", "not available")
        with patch("repvblicvs_engine.executor.refresh_sol", return_value=None), patch("repvblicvs_engine.executor._run") as run:
            with self.assertRaises(WorkflowError):
                run_model_instruction(self.store, self.task, self.output)
        run.assert_not_called()
        self.router.configure_provider("sol", "openai", model="gpt-6.1-sol", status="ready", verified_until=self.until, evidence={"auth": "api"})
        with patch("repvblicvs_engine.executor.refresh_sol") as refresh, patch("repvblicvs_engine.executor._run") as run:
            with self.assertRaises(WorkflowError):
                run_model_instruction(self.store, self.task, self.output)
        refresh.assert_not_called()
        run.assert_not_called()

    def test_postflight_reserve_exhaustion_stops_new_credit_work(self):
        self.authorize_existing_credits()
        with patch("repvblicvs_engine.allowance.read_live_limits", side_effect=[credit_limits(), credit_limits("49")]), patch("repvblicvs_engine.executor._run", return_value=sol_result()) as run, patch("repvblicvs_engine.executor.command", return_value=["fake"]):
            first = run_model_instruction(self.store, self.task, self.output)
        self.assertFalse(first["evidence"][0]["allowance_postflight"]["allowed"])
        task = self.store.enqueue({"kind": "model_instruction", "payload": {"prompt": "Second synthetic request", "preferred": "sol"}})
        with patch("repvblicvs_engine.allowance.read_live_limits", return_value=credit_limits("49")), patch("repvblicvs_engine.executor._run") as again:
            with self.assertRaises(WorkflowError):
                run_model_instruction(self.store, task, self.output)
        again.assert_not_called()
        self.assertEqual(run.call_count, 1)

    def test_failed_credit_call_retains_unattributed_depletion_and_mode(self):
        self.authorize_existing_credits()
        failure = {"exit_code": -9, "stdout": "partial", "stderr": "", "failure": "timeout"}
        with patch("repvblicvs_engine.allowance.read_live_limits", side_effect=[credit_limits(), credit_limits("119")]), patch("repvblicvs_engine.executor._run", return_value=failure) as run, patch("repvblicvs_engine.executor.command", return_value=["fake"]):
            for _ in range(2):
                with self.assertRaises(WorkflowError):
                    run_model_instruction(self.store, self.task, self.output)
        receipt = json.loads(next((self.store.root / "model_calls").glob("*.json")).read_text())
        self.assertEqual(receipt["status"], "unknown")
        self.assertEqual(receipt["billing_mode"], "authorized_existing_credits")
        self.assertEqual(receipt["observed_shared_account_depletion_since_preflight_credits"], 1)
        self.assertEqual(run.call_count, 1)

    def test_post_spawn_io_error_is_ambiguous_and_never_replayed(self):
        self.authorize_existing_credits()
        with patch("repvblicvs_engine.allowance.read_live_limits", side_effect=[credit_limits(), credit_limits("119")]), patch("repvblicvs_engine.executor._run", side_effect=OSError("synthetic post-spawn pipe failure")) as run, patch("repvblicvs_engine.executor.command", return_value=["fake"]):
            for _ in range(2):
                with self.assertRaisesRegex(WorkflowError, "ambiguous"):
                    run_model_instruction(self.store, self.task, self.output)
        self.assertEqual(run.call_count, 1)
        receipt = json.loads(next((self.store.root / "model_calls").glob("*.json")).read_text())
        self.assertEqual(receipt["status"], "unknown")
        self.assertEqual(receipt["observed_shared_account_depletion_since_preflight_credits"], 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM reservations").fetchone()[0], "reserved")

    def test_true_spawn_failure_releases_no_call_reservation(self):
        self.authorize_existing_credits()
        with patch("repvblicvs_engine.allowance.read_live_limits", return_value=credit_limits()) as read, patch("repvblicvs_engine.executor._run", side_effect=ProcessNotStarted("synthetic executable missing")), patch("repvblicvs_engine.executor.command", return_value=["fake"]):
            with self.assertRaises(WorkflowError):
                run_model_instruction(self.store, self.task, self.output)
        self.assertEqual(read.call_count, 1)
        receipt = json.loads(next((self.store.root / "model_calls").glob("*.json")).read_text())
        self.assertEqual(receipt["status"], "failed")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM reservations").fetchone()[0], "released")

    def test_real_post_spawn_pipe_error_differs_from_creation_failure(self):
        original_popen, created, pipe_patchers = subprocess.Popen, [], []
        def launch_then_break_pipe(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            created.append(process)
            patcher = patch("repvblicvs_engine.executor.os.read", side_effect=OSError("synthetic pipe error"))
            patcher.start()
            pipe_patchers.append(patcher)
            return process
        try:
            with patch("repvblicvs_engine.executor.subprocess.Popen", side_effect=launch_then_break_pipe):
                with self.assertRaises(OSError) as raised:
                    _run([sys.executable, "-c", "print('synthetic response')"], "", self.output, 5, 10000)
        finally:
            for patcher in pipe_patchers:
                patcher.stop()
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].returncode)
        self.assertNotIsInstance(raised.exception, ProcessNotStarted)
        with patch("repvblicvs_engine.executor.subprocess.Popen", side_effect=OSError("synthetic creation error")):
            with self.assertRaises(ProcessNotStarted):
                _run(["missing"], "", self.output, 5, 10000)

    def test_copilot_reports_actual_auto_model_with_free_gate(self):
        self.router.unavailable("opus", "not available")
        self.router.unavailable("sol", "not available")
        self.router.configure_provider("copilot", "github", model="auto", status="ready", verified_until=self.until, evidence={"plan": "free", "auth": "github", "paid_usage_disabled": True, "remaining_ai_credits": 196, "observed_at": time.time() - 1})
        response = {"exit_code": 0, "stdout": "Artifact review", "stderr": "", "failure": None, "copilot_usage": {"currentModel": "mai-code-1.1-flash", "totalNanoAiu": 270000000, "codeChanges": {"filesModifiedCount": 0}}}
        with patch("repvblicvs_engine.executor._run", return_value=response), patch("repvblicvs_engine.executor.command", return_value=["fake"]):
            result = run_model_instruction(self.store, self.task, self.output)
        self.assertEqual(result["summary"]["model"], "mai-code-1.1-flash")
        self.assertEqual(result["evidence"][0]["ai_credits"], 0.27)

    def test_task_cannot_supply_commands_and_call_is_bounded(self):
        self.task["payload"]["command"] = "touch something"
        with patch("repvblicvs_engine.executor._run") as run:
            with self.assertRaises(WorkflowError):
                run_model_instruction(self.store, self.task, self.output)
        run.assert_not_called()
        with patch("repvblicvs_engine.executor.shutil.which", return_value="/fake/claude"), patch("repvblicvs_engine.executor.codex_binary", return_value="/fake/codex"):
            opus, sol = command("opus"), command("sol")
        self.assertEqual(opus[opus.index("--tools") + 1], "")
        self.assertIn("--safe-mode", opus)
        self.assertEqual(sol[1:], ["app-server", "--listen", "stdio://"])

    def test_explicit_chatgpt_model_rejection_is_capability_and_falls_back(self):
        self.authorize_existing_credits()
        failure = {"exit_code": 1, "stdout": "", "stderr": "HTTP 400: The gpt-6.1-sol model is not supported when using Codex with a ChatGPT account", "failure": None}
        self.assertEqual(classify_failure(failure), "capability")
        self.router.configure_provider("opus", "claude", model="claude-opus-5-5", status="ready", verified_until=self.until, evidence={"paid_usage_disabled": True, "auto_reload_disabled": True, "auth": "oauth"})
        with patch("repvblicvs_engine.allowance.read_live_limits", side_effect=[credit_limits(), credit_limits()]), patch("repvblicvs_engine.executor._run", side_effect=[failure, opus_result()]), patch("repvblicvs_engine.executor.command", side_effect=lambda *args: [args[0]]):
            result = run_model_instruction(self.store, self.task, self.output)
        self.assertEqual(result["summary"]["provider"], "opus")
        receipts = [json.loads(path.read_text()) for path in (self.store.root / "model_calls").glob("*.json")]
        failed = next(receipt for receipt in receipts if receipt["provider"] == "sol")
        self.assertEqual(failed["classification"], "capability")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["billing_mode"], "authorized_existing_credits")

    def test_real_subprocess_output_cap_and_deadline(self):
        result = _run([sys.executable, "-c", "print('x'*200000)"], "", self.output, 5, 10000)
        self.assertEqual(result["failure"], "output_limit")
        self.assertLessEqual(len(result["stdout"]), 10000)
        result = _run([sys.executable, "-c", "import time;time.sleep(10)"], "", self.output, 0.1, 10000)
        self.assertEqual(result["failure"], "timeout")

    def test_child_keeps_inherited_account_lock(self):
        lock_path = self.store.root / "test-inherited.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            script = "import fcntl,os,sys; inherited=int(sys.argv[1]); fd=os.open(sys.argv[2],os.O_RDWR);\ntry: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); print('LOST_LOCK')\nexcept BlockingIOError: print('LOCK_RETAINED')"
            result = _run([sys.executable, "-c", script, str(descriptor), str(lock_path)], "", self.output, 5, 10000, (descriptor,))
            self.assertEqual(result["stdout"].strip(), "LOCK_RETAINED")
            self.assertEqual(result["exit_code"], 0)
        finally:
            os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
