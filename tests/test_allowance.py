import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from repvblicvs_engine.allowance import dispatch_decision, normalize, observe_credit_balance, read_live_limits, refresh_sol, routing_decision, safe_environment
from repvblicvs_engine.providers import Router
from repvblicvs_engine.store import Store


def limits(now, short=15, week=30):
    return {"rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": short, "windowDurationMins": 300, "resetsAt": now + 1000}, "secondary": {"usedPercent": week, "windowDurationMins": 10080, "resetsAt": now + 10000}}}, "accountId": "never-retain", "credits": {"privateId": "never-retain"}}


def credit_limits(now, balance="120", short=100, week=36):
    raw = limits(now, short, week)
    raw["ordinaryUsageAllowed"] = short < 100 and week < 100
    raw["rateLimitsByLimitId"]["codex"].update(credits={"hasCredits": True, "balance": balance, "unlimited": False, "privateId": "never-retain"}, spendControlReached=False, rateLimitReachedType="rate_limit_reached" if short >= 100 or week >= 100 else None)
    return raw


def owner_policy(initial=120):
    return {"authorized": True, "scope": "existing_codex_balance", "initial_balance": initial, "reserve_credits": 50, "max_balance_to_use": initial - 50, "authorization_receipt": "synthetic-owner-direct-receipt", "purchases_allowed": False, "refill_allowed": False}


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

    def test_credit_parser_preserves_units_and_quota_vs_hard_block(self):
        now = time.time()
        parsed = normalize(credit_limits(now, "119.1250"), now)
        bucket = parsed["buckets"]["codex"]
        self.assertEqual(bucket["credits"]["balance"], 119.125)
        self.assertEqual(bucket["credits"]["unit"], "codex_credits")
        self.assertFalse(bucket["ordinary_usage_allowed"])
        self.assertFalse(bucket["provider_blocked"])
        self.assertEqual(bucket["rate_limit_reached_type"], "rate_limit_reached")
        self.assertNotIn("never-retain", json.dumps(parsed))
        self.assertFalse(dispatch_decision(parsed, now=now)[0])
        self.assertEqual(routing_decision(parsed, now=now, credit_policy=owner_policy())["billing_mode"], "authorized_existing_credits")

    def test_credits_without_trusted_owner_authorization_are_rejected(self):
        now = time.time()
        parsed = normalize(credit_limits(now), now)
        for key, value in (("authorized", False), ("authorized", 1), ("scope", "new_credits"), ("purchases_allowed", True), ("refill_allowed", True), ("reserve_credits", -1), ("initial_balance", True), ("max_balance_to_use", float("nan")), ("max_balance_to_use", 71), ("authorization_receipt", "")):
            with self.subTest(key=key, value=value):
                self.assertFalse(dispatch_decision(parsed, now=now, credit_policy=owner_policy() | {key: value})[0])
        self.assertFalse(dispatch_decision(parsed, now=now)[0])

    def test_existing_credits_are_fallback_and_do_not_relax_included_reserves(self):
        now = time.time()
        included = normalize(credit_limits(now, short=15, week=30), now)
        reserved = normalize(credit_limits(now, short=90, week=30), now)
        self.assertEqual(routing_decision(included, now=now, credit_policy=owner_policy())["billing_mode"], "included")
        self.assertFalse(routing_decision(reserved, now=now, credit_policy=owner_policy())["allowed"])
        self.assertFalse(dispatch_decision(reserved, now=now)[0])

    def test_hard_unknown_malformed_and_stale_blocks_cannot_use_credit_balance(self):
        now = time.time()
        for key, value in (("spendControlReached", True), ("spendControlReached", "false"), ("spendControlReached", None), ("rateLimitReachedType", "spend_control_reached"), ("rateLimitReachedType", "future_unknown_limit"), ("rateLimitReachedType", False), ("rateLimitReachedType", None)):
            with self.subTest(key=key, value=value):
                raw = credit_limits(now)
                raw["rateLimitsByLimitId"]["codex"][key] = value
                self.assertFalse(dispatch_decision(normalize(raw, now), now=now, credit_policy=owner_policy())[0])
        for ordinary in (True, "false", 0, 1):
            raw = credit_limits(now)
            raw["ordinaryUsageAllowed"] = ordinary
            self.assertFalse(dispatch_decision(normalize(raw, now), now=now, credit_policy=owner_policy())[0])
        for balance in ("NaN", "infinity", "garbage", True, -1, None, "50", "0"):
            with self.subTest(balance=balance):
                self.assertFalse(dispatch_decision(normalize(credit_limits(now, balance), now), now=now, credit_policy=owner_policy())[0])
        for observed in (now - 31, now + 1, True):
            self.assertFalse(dispatch_decision(normalize(credit_limits(now), observed), now=now, credit_policy=owner_policy())[0])
        for window in (None, {"usedPercent": 101, "windowDurationMins": 300, "resetsAt": now + 1000}, {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": now - 1}):
            raw = credit_limits(now)
            raw["rateLimitsByLimitId"]["codex"]["primary"] = window
            self.assertFalse(dispatch_decision(normalize(raw, now), now=now, credit_policy=owner_policy())[0])

    def test_credit_ceiling_and_reserve_equality_block_new_dispatch(self):
        now = time.time()
        parsed = normalize(credit_limits(now, "1000"), now)
        self.assertTrue(dispatch_decision(parsed, now=now, credit_policy=owner_policy(), observed_depletion_credits=69.9)[0])
        self.assertFalse(dispatch_decision(parsed, now=now, credit_policy=owner_policy(), observed_depletion_credits=70)[0])
        self.assertFalse(dispatch_decision(parsed, now=now, credit_policy=owner_policy(), observed_depletion_credits=float("nan"))[0])
        self.assertFalse(dispatch_decision(parsed, now=now, credit_policy=owner_policy(), observed_depletion_credits=True)[0])

    def test_explicit_owner_zero_reserve_uses_only_authorized_existing_balance(self):
        now = time.time()
        policy = owner_policy() | {"reserve_credits": 0, "max_balance_to_use": 120}
        parsed = normalize(credit_limits(now, "0.25"), now)
        decision = routing_decision(parsed, now=now, credit_policy=policy, observed_depletion_credits=119.75)
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["credit_reserve"], 0)
        self.assertEqual(decision["remaining_authorized_credits"], 0.25)
        self.assertFalse(routing_decision(parsed, now=now)["allowed"])
        self.assertFalse(routing_decision(parsed, now=now, credit_policy=policy | {"purchases_allowed": True})["allowed"])
        self.assertFalse(routing_decision(parsed, now=now, credit_policy=policy | {"refill_allowed": True})["allowed"])
        self.assertFalse(routing_decision(normalize(credit_limits(now, "0"), now), now=now, credit_policy=policy)["allowed"])
        self.assertFalse(routing_decision(parsed, now=now, credit_policy=policy, observed_depletion_credits=120)["allowed"])

    def test_cumulative_unattributed_credit_ledger_survives_refill_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with store.connection(write=True) as db:
                db.execute("INSERT INTO meta VALUES('codex_credit_policy',?)", (json.dumps(owner_policy()),))
            now = time.time() - 1
            for index, (balance, expected) in enumerate((("110", 10), ("210", 10), ("160", 60), ("150", 70))):
                snapshot = normalize(credit_limits(now, balance), now + index * 0.1)
                policy, usage = observe_credit_balance(Store(directory), snapshot)
                self.assertEqual(usage["observed_depletion_credits"], expected)
                self.assertEqual(usage["attribution"], "shared_account_unattributed")
            self.assertFalse(dispatch_decision(snapshot, credit_policy=policy, observed_depletion_credits=usage["observed_depletion_credits"])[0])
            self.assertEqual(len([event for event in store.events() if event["kind"] == "codex_credit_balance_observed"]), 4)
            self.assertEqual(store.status()["recorded_paid_cost"], 0)
            # Observation replay is idempotent, not an additional debit.
            self.assertEqual(observe_credit_balance(store, snapshot)[1]["observed_depletion_credits"], 70)
            with store.connection(write=True) as db:
                db.execute("UPDATE meta SET value=? WHERE key='codex_credit_policy'", (json.dumps(owner_policy(220)),))
            with self.assertRaisesRegex(RuntimeError, "authorization changed"):
                observe_credit_balance(store, snapshot)

    def test_refresh_routes_with_credit_evidence_but_no_fake_dollar_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            store, now = Store(directory), time.time()
            router = Router(store)
            with store.connection(write=True) as db:
                db.execute("INSERT INTO meta VALUES('codex_credit_policy',?)", (json.dumps(owner_policy()),))
            router.configure_account("openai", evidence={"source": "synthetic fixture"}, verified_until=now + 60)
            router.configure_provider("sol", "openai", model="gpt-6.1-sol", evidence={"auth": "chatgpt"}, status="ready", verified_until=now + 60)
            provider = next(item for item in router.list() if item["name"] == "sol")
            with patch("repvblicvs_engine.allowance.read_live_limits", return_value=credit_limits(now, "110")):
                snapshot = refresh_sol(store, router, provider)
            updated = next(item for item in router.list() if item["name"] == "sol")
            self.assertTrue(updated["currently_verified"])
            self.assertEqual(updated["billing_mode"], "authorized_existing_credits")
            self.assertEqual(updated["available_credit"], 0)  # monetary pool remains zero
            self.assertEqual(snapshot["credit_observation"]["observed_depletion_credits"], 10)
            with patch("repvblicvs_engine.allowance.read_live_limits", return_value=credit_limits(now, "50")):
                after = refresh_sol(store, router, provider, raise_on_unavailable=False)
            self.assertFalse(after["dispatch"]["allowed"])
            self.assertEqual(next(item for item in router.list() if item["name"] == "sol")["account_status"], "exhausted")

    def test_corrupt_ledger_never_reinitializes_or_restores_credit_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with store.connection(write=True) as db:
                db.execute("INSERT INTO meta VALUES('codex_credit_policy',?)", (json.dumps(owner_policy()),))
            policy, retained = observe_credit_balance(store, normalize(credit_limits(time.time(), "60")))
            snapshot = normalize(credit_limits(time.time(), "210"))
            invalid_ledgers = ({}, [], None, False, 0, retained | {"observed_depletion_credits": True}, retained | {"unit": "usd"}, retained | {"last_observed_balance": float("nan")}, retained | {"remaining_original_authorized_credits": 70}, retained | {"last_observed_at": time.time() + 60})
            for invalid in invalid_ledgers:
                with self.subTest(invalid=invalid):
                    with store.connection(write=True) as db:
                        db.execute("UPDATE meta SET value=? WHERE key='codex_credit_usage'", (json.dumps(invalid),))
                    with self.assertRaisesRegex(RuntimeError, "ledger"):
                        observe_credit_balance(store, snapshot)

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
