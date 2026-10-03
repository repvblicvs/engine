import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from repvblicvs_engine.allowance import dispatch_decision, normalize, read_live_limits, safe_environment


def limits(now, short=15, week=30):
    return {"rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": short, "windowDurationMins": 300, "resetsAt": now + 1000}, "secondary": {"usedPercent": week, "windowDurationMins": 10080, "resetsAt": now + 10000}}}, "accountId": "never-retain", "credits": {"privateId": "never-retain"}}


class AllowanceTests(unittest.TestCase):
    def test_modern_parser_preserves_windows_and_discards_identifiers(self):
        now = time.time()
        parsed = normalize(limits(now), now)
        self.assertTrue(dispatch_decision(parsed, now=now)[0])
        self.assertEqual(parsed["buckets"]["codex"]["windows"][0]["remaining_percent"], 85)
        self.assertNotIn("never-retain", json.dumps(parsed))

    def test_legacy_parser(self):
        now = time.time()
        raw = {"rateLimits": limits(now)["rateLimitsByLimitId"]["codex"]}
        self.assertTrue(dispatch_decision(normalize(raw, now), now=now)[0])

    def test_owner_reserves_and_staleness_block_launch(self):
        now = time.time()
        for short, week in ((90, 10), (89, 80), (100, 100)):
            self.assertFalse(dispatch_decision(normalize(limits(now, short, week), now), now=now)[0])
        parsed = normalize(limits(now), now - 31)
        self.assertFalse(dispatch_decision(parsed, now=now)[0])
        self.assertFalse(dispatch_decision(normalize(limits(now), now + 1), now=now)[0])

    def test_missing_malformed_and_expired_windows_are_unavailable(self):
        now = time.time()
        for used in (True, float("nan"), "10", None):
            raw = limits(now)
            raw["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = used
            self.assertFalse(dispatch_decision(normalize(raw, now), now=now)[0])
        raw = limits(now)
        raw["rateLimitsByLimitId"]["codex"]["primary"]["resetsAt"] = now - 1
        self.assertFalse(dispatch_decision(normalize(raw, now), now=now)[0])

    def test_provider_blocked(self):
        now = time.time()
        raw = limits(now)
        raw["rateLimitsByLimitId"]["codex"]["spendControlReached"] = True
        self.assertFalse(dispatch_decision(normalize(raw, now), now=now)[0])

    def test_paid_api_environment_is_removed(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret", "ANTHROPIC_BASE_URL": "url", "OPENAI_API_KEY": "secret", "GH_TOKEN": "secret", "CLAUDE_CODE_USE_BEDROCK": "1", "HOME": "/oauth/home"}):
            clean = safe_environment()
        self.assertNotIn("ANTHROPIC_API_KEY", clean)
        self.assertNotIn("OPENAI_API_KEY", clean)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", clean)
        self.assertNotIn("GH_TOKEN", clean)
        self.assertEqual(clean["HOME"], "/oauth/home")

    def test_non_inference_protocol_handshake_and_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "fake-codex"
            raw = limits(time.time())
            binary.write_text(f"#!{sys.executable}\nimport json,sys\nfor line in sys.stdin:\n request=json.loads(line)\n method=request['method']\n if method not in ('initialize','account/rateLimits/read'): raise RuntimeError('inference forbidden')\n result={{}} if method=='initialize' else {raw!r}\n print(json.dumps({{'id':request['id'],'result':result}}),flush=True)\n")
            binary.chmod(0o700)
            with patch("repvblicvs_engine.allowance.codex_binary", return_value=str(binary)):
                self.assertEqual(read_live_limits(2), raw)
            binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(10)\n")
            with patch("repvblicvs_engine.allowance.codex_binary", return_value=str(binary)):
                with self.assertRaises(RuntimeError):
                    read_live_limits(0.1)


if __name__ == "__main__":
    unittest.main()
