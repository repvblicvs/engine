import json
import fcntl
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from repvblicvs_engine.executor import _run, command, run_model_instruction
from repvblicvs_engine.providers import Router
from repvblicvs_engine.store import Store
from repvblicvs_engine.workflows import WorkflowError


def opus_result(answer="REVIEW_READY"):
    return {"exit_code": 0, "stdout": json.dumps({"result": answer, "is_error": False, "total_cost_usd": 0.02, "usage": {"output_tokens": 8}}), "stderr": "", "failure": None}


def sol_result(answer="FALLBACK_READY"):
    return {"exit_code": 0, "stdout": "\n".join(json.dumps(event) for event in ({"type": "item.completed", "item": {"type": "agent_message", "text": answer}}, {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}})), "stderr": "", "failure": None}


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
        with patch("repvblicvs_engine.executor._run", side_effect=[failure, sol_result()]) as run, patch("repvblicvs_engine.executor.refresh_sol") as refresh, patch("repvblicvs_engine.executor.command", side_effect=lambda *args: [args[0]]):
            result = run_model_instruction(self.store, self.task, self.output)
        self.assertEqual(result["summary"]["provider"], "sol")
        self.assertEqual(run.call_args_list[0].args[1], run.call_args_list[1].args[1])
        self.assertEqual(refresh.call_count, 1)
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
        with patch("repvblicvs_engine.executor.refresh_sol", side_effect=refresh) as preflight, patch("repvblicvs_engine.executor._run", return_value=sol_result()), patch("repvblicvs_engine.executor.command", return_value=["fake"]):
            result = run_model_instruction(self.store, self.task, self.output)
        self.assertEqual(result["summary"]["provider"], "sol")
        self.assertEqual(preflight.call_count, 1)

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
        self.assertIn("--ignore-user-config", sol)
        self.assertIn("shell_tool", sol)
        self.assertEqual(sol[sol.index("--sandbox") + 1], "read-only")

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
