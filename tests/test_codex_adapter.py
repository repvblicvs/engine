import base64
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from repvblicvs_engine.codex_adapter import AdapterError, DISABLED_FEATURES, MODEL, Rpc, isolated_context, run


class CodexAdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name)
        claims = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode()).decode().rstrip("=")
        self.auth = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {"access_token": "synthetic." + claims + ".synthetic", "id_token": "synthetic-id", "refresh_token": "synthetic-refresh", "account_id": "synthetic-account"}}
        (self.source / "auth.json").write_text(json.dumps(self.auth))
        (self.source / "auth.json").chmod(0o600)
        self.catalog = {"models": [{"slug": MODEL, "use_responses_lite": True, "tool_mode": "code_mode_only", "supported_in_api": True, "supported_reasoning_levels": [{"effort": "low"}], "experimental_supported_tools": ["unwanted"]}]}
        (self.source / "models_cache.json").write_text(json.dumps(self.catalog))
        (self.source / "AGENTS.md").write_text("PRIVATE INSTRUCTIONS MUST NOT BE COPIED")
        (self.source / "config.toml").write_text("PRIVATE MCP AND HOOK SETTINGS MUST NOT BE COPIED")
        self.server = self.source / "fake-server"

    def server_script(self, fault=""):
        script = f'''#!{sys.executable}
import json,os,sys
fault={fault!r}
features={list(DISABLED_FEATURES)!r}
def emit(x): print(json.dumps(x),flush=True)
for line in sys.stdin:
 r=json.loads(line);m=r.get('method');i=r.get('id');params=r.get('params',{{}})
 if m=='initialized': continue
 if m=='initialize': result={{}}
 elif m=='model/list': result={{'data':[{{'model':{MODEL!r}}}],'nextCursor':None}}
 elif m=='experimentalFeature/list': result={{'data':[{{'name':n,'enabled':n=='hooks' and fault=='feature'}} for n in features]+[{{'name':'unified_exec','enabled':True}}],'nextCursor':None}}
 elif m=='mcpServerStatus/list': result={{'data':[{{'name':'unexpected'}}] if fault=='mcp' else [],'nextCursor':None}}
 elif m=='thread/start':
  assert params['environments']==[] and params['runtimeWorkspaceRoots']==[] and params['dynamicTools']==[] and params['allowProviderModelFallback']==False
  result={{'model':{MODEL!r},'modelProvider':'openai','approvalPolicy':'never','sandbox':{{'type':'readOnly','networkAccess':False}},'instructionSources':['private'] if fault=='instructions' else [],'runtimeWorkspaceRoots':[],'reasoningEffort':'low','serviceTier':'default','cwd':params['cwd'],'thread':{{'id':'fixture-thread','ephemeral':True,'model':{MODEL!r},'modelProvider':'openai','cwd':params['cwd'],'environments':[]}}}}
 elif m=='turn/start':
  assert params['environments']==[] and params['runtimeWorkspaceRoots']==[] and params['model']=={MODEL!r}
  open({str(self.source / 'turn-dispatched')!r},'w').write('once')
  if fault=='disconnect': sys.exit(0)
  if fault=='reject': emit({{'id':i,'error':{{'code':-32602,'message':'The gpt-6.1-sol model is not supported when using Codex with a ChatGPT account'}}}});continue
  if fault=='tool': emit({{'id':900,'method':'item/tool/call','params':{{}}}});continue
  if fault=='reroute': emit({{'method':'model/rerouted','params':{{'fromModel':{MODEL!r},'toModel':'another-model'}}}});continue
  if fault.startswith('settings-'):
   field=fault[len('settings-'):]
   changed={{'model':'another-model','modelProvider':'another-provider','approvalPolicy':'on-request','sandboxPolicy':{{'type':'dangerFullAccess'}},'cwd':'/unexpected'}}[field]
   emit({{'method':'thread/settings/updated','params':{{'threadId':'fixture-thread','threadSettings':{{field:changed}}}}}});continue
  if fault=='activity': emit({{'method':'item/started','params':{{'threadId':'fixture-thread','turnId':'fixture-turn','item':{{'id':'tool','type':'commandExecution'}}}}}});continue
  item={{'id':'answer','type':'agentMessage','phase':'final_answer','text':'SYNTHETIC_READY'}}
  emit({{'method':'item/completed','params':{{'threadId':'fixture-thread','turnId':'fixture-turn','item':item}}}})
  emit({{'method':'item/completed','params':{{'threadId':'fixture-thread','turnId':'fixture-turn','item':item}}}})
  emit({{'method':'thread/tokenUsage/updated','params':{{'threadId':'fixture-thread','tokenUsage':{{'outputTokens':3}}}}}})
  emit({{'method':'turn/completed','params':{{'threadId':'fixture-thread','turn':{{'id':'fixture-turn','status':'failed' if fault=='partial-quota' else 'completed','error':{{'message':'usage limit reached'}} if fault=='partial-quota' else None}}}}}})
  result={{'turn':{{'id':'fixture-turn','status':'inProgress'}}}}
 else: raise RuntimeError('unexpected RPC method')
 emit({{'id':i,'result':result}})
'''
        self.server.write_text(script)
        self.server.chmod(0o700)
        return [str(self.server)]

    def test_context_has_only_private_oauth_vetted_catalog_and_config_then_removed(self):
        original = (self.source / "auth.json").read_bytes()
        with isolated_context(self.source) as context:
            copied = Path(context["environment"]["CODEX_HOME"])
            self.assertEqual(set(path.name for path in copied.iterdir()), {"auth.json", "sol-catalog.json", "config.toml"})
            self.assertEqual(copied.stat().st_mode & 0o777, 0o700)
            self.assertEqual((copied / "auth.json").stat().st_mode & 0o777, 0o600)
            metadata = json.loads((copied / "sol-catalog.json").read_text())["models"][0]
            self.assertEqual(metadata["slug"], MODEL)
            self.assertTrue(metadata["use_responses_lite"])
            self.assertEqual(metadata["shell_type"], "disabled")
            self.assertEqual(metadata["experimental_supported_tools"], [])
            self.assertNotIn("PRIVATE", (copied / "config.toml").read_text())
        self.assertFalse(copied.exists())
        self.assertEqual((self.source / "auth.json").read_bytes(), original)

    def test_api_key_malformed_oauth_and_symlink_context_are_refused(self):
        for changes in ({"OPENAI_API_KEY": "synthetic-noncredential"}, {"auth_mode": "api"}, {"tokens": {}}):
            (self.source / "auth.json").write_text(json.dumps(self.auth | changes))
            with self.assertRaises(AdapterError):
                with isolated_context(self.source):
                    self.fail("unsafe context accepted")
        (self.source / "auth.json").unlink()
        (self.source / "auth-copy.json").write_text(json.dumps(self.auth))
        (self.source / "auth.json").symlink_to(self.source / "auth-copy.json")
        with self.assertRaises(OSError):
            with isolated_context(self.source):
                self.fail("symlink context accepted")

    def test_inspection_never_dispatches_turn(self):
        result = run(self.server_script(), "No inference", 5, 10000, inspection_only=True, source_home=self.source)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["sol_adapter_evidence"]["configured_model"], MODEL)
        self.assertFalse((self.source / "turn-dispatched").exists())

    def test_notifications_before_turn_response_complete_once_and_keep_token_units(self):
        result = run(self.server_script(), "Synthetic packet", 5, 10000, source_home=self.source, allowance_observed_at=time.time())
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].count("SYNTHETIC_READY"), 1)
        self.assertEqual(result["sol_adapter_evidence"]["turn_id"], "fixture-turn")
        self.assertNotIn("synthetic-refresh", json.dumps(result))

    def test_failed_isolation_and_stale_allowance_never_dispatch(self):
        for fault in ("feature", "mcp", "instructions", ""):
            with self.subTest(fault=fault):
                result = run(self.server_script(fault), "Synthetic packet", 5, 10000, source_home=self.source, allowance_observed_at=time.time() - 31)
                self.assertEqual(result["exit_code"], 1)
                self.assertIsNone(result["failure"])
                self.assertFalse((self.source / "turn-dispatched").exists())

    def test_after_dispatch_tool_reroute_activity_and_disconnect_remain_ambiguous(self):
        for fault in ("tool", "reroute", "activity", "disconnect", "settings-model", "settings-modelProvider", "settings-approvalPolicy", "settings-sandboxPolicy", "settings-cwd", "partial-quota"):
            with self.subTest(fault=fault):
                result = run(self.server_script(fault), "Synthetic packet", 5, 10000, source_home=self.source, allowance_observed_at=time.time())
                self.assertEqual(result["failure"], "ambiguous_app_server")
                self.assertTrue((self.source / "turn-dispatched").exists())
                if fault == "partial-quota":
                    self.assertIn("SYNTHETIC_READY", result["stdout"])
                    self.assertIn("sol.partial_event", result["stdout"])

    def test_explicit_unsupported_model_refusal_is_not_ambiguous(self):
        result = run(self.server_script("reject"), "Synthetic packet", 5, 10000, source_home=self.source, allowance_observed_at=time.time())
        self.assertEqual(result["exit_code"], 1)
        self.assertIsNone(result["failure"])
        self.assertIn("not supported", result["stderr"])

    def test_stalled_child_stdin_respects_monotonic_deadline(self):
        process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(5)"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rpc = Rpc(process, time.monotonic() + .1, 10000)
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(AdapterError, "stdin deadline"):
                rpc.send({"input": "x" * 1_000_000})
            self.assertLess(time.monotonic() - started, 1)
        finally:
            rpc.selector.close()
            process.kill()
            process.communicate()

    def test_dispatcher_setup_failure_reaps_child_before_return(self):
        original = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            children.append(process)
            return process
        with patch("repvblicvs_engine.codex_adapter.subprocess.Popen", side_effect=spawn), patch("repvblicvs_engine.codex_adapter.Rpc", side_effect=OSError("synthetic selector failure")):
            result = run(self.server_script(), "Synthetic packet", 5, 10000, source_home=self.source, allowance_observed_at=time.time())
        self.assertEqual(result["exit_code"], 1)
        self.assertIsNone(result["failure"])
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
