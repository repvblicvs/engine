"""Synthetic native receipts; no Google login, quota query or inference."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import pytest

from repvblicvs_engine import agy_adapter as agy


def settings():
    return {"useG1Credits": False, "enableTelemetry": False, "permissions": {"allow": [], "ask": [], "deny": sorted(agy.DENIALS)}}


def native_result(response="ARTIFACT_READY", *, status="SUCCESS", model=agy.MODEL, step_type="agent_response"):
    events = [
        {"event": "init", "conversation_id": "synthetic", "init": {"model": model, "permission_mode": "request-review", "cwd": "/synthetic/private", "tools": ["advertised_but_denied_tool"]}},
        {"event": "step_update", "step_update": {"conversation_id": "synthetic", "step_index": 0, "step_type": "user_input", "state": "DONE"}},
        {"event": "step_update", "step_update": {"conversation_id": "synthetic", "step_index": 1, "step_type": step_type, "state": "DONE", "text_delta": response}},
        {"event": "result", "result": {"status": status, "response": response, "num_turns": 1, "duration_seconds": 2, "usage": {"input_tokens": 2, "output_tokens": 3}}},
    ]
    return {"exit_code": 0, "stdout": "\n".join(json.dumps(event) for event in events), "stderr": "", "failure": None}


@pytest.fixture
def context(tmp_path, monkeypatch):
    profile = tmp_path / "settings.json"
    profile.write_text(json.dumps(settings()))
    profile.chmod(0o600)
    original = agy.profile_snapshot
    monkeypatch.setattr(agy, "profile_snapshot", lambda **kwargs: original(profile, **kwargs))
    now = time.time()
    snapshot = original(profile)
    provider = {"model": agy.MODEL, "remaining_calls": 5,
                "evidence": {"auth": "google_cached_oauth", "cli_version": agy.CLI_VERSION, "profile_verified": True, "profile_verified_at": now, "profile_receipt": "synthetic-effective-config", "profile_sha256": snapshot["profile_sha256"], "use_g1_credits": False},
                "account_evidence": {"billing_mode": "included", "use_g1_credits": False, "credits_off_receipt": "synthetic-native-credit-control", "native_allowance": {"models": [agy.MODEL], "remaining_percent": 80, "observed_at": now, "reset_at": now + 1800, "receipt": "synthetic-native-usage"}}}
    return profile, provider, snapshot, now


def test_effective_profile_and_native_allowance_admit_bounded_artifact(context):
    _, provider, _, now = context
    proof = agy.preflight(provider, now=now)
    assert proof["included_allowance"] and proof["paid_charge_recorded"] == 0
    assert not proof["tool_inventory_removed"] and not proof["native_quota_is_call_count"]
    result = native_result()
    result["agy_preflight"] = proof
    answer, evidence = agy.response(result)
    assert answer == "ARTIFACT_READY" and evidence["model"] == agy.MODEL
    assert evidence["adapter"]["terminal_status"] == "SUCCESS"
    assert evidence["adapter"]["tool_events_observed"] == evidence["adapter"]["delegation_events_observed"] == 0
    assert evidence["adapter"]["tools_advertised"] is True
    assert evidence["adapter"]["plan_mode_is_security_boundary"] is False
    assert evidence["native_stream_sha256"] == hashlib.sha256(result["stdout"].encode()).hexdigest()


@pytest.mark.parametrize("field,value", [
    ("auth", "api"), ("cli_version", "unverified"), ("profile_verified", False),
    ("profile_receipt", ""), ("profile_sha256", "different"), ("use_g1_credits", True),
])
def test_unverified_profile_or_billing_never_admits_route(context, field, value):
    _, provider, _, now = context
    provider["evidence"][field] = value
    with pytest.raises(agy.AdapterError): agy.preflight(provider, now=now)


@pytest.mark.parametrize("where,field,value", [
    ("evidence", "profile_verified_at", -3601),
    ("evidence", "profile_verified_at", 1),
    ("quota", "observed_at", -3601),
    ("quota", "observed_at", 1),
    ("quota", "reset_at", -1),
])
def test_stale_future_or_expired_native_receipts_are_refused(context, where, field, value):
    _, provider, _, now = context
    target = provider["evidence"] if where == "evidence" else provider["account_evidence"]["native_allowance"]
    target[field] = now + value
    with pytest.raises(agy.AdapterError): agy.preflight(provider, now=now)


@pytest.mark.parametrize("changes", [
    {"billing_mode": "credits"}, {"use_g1_credits": True}, {"credits_off_receipt": ""},
    {"native_allowance": {}}, {"native_allowance": None},
])
def test_account_balance_or_missing_native_quota_does_not_grant_allowance(context, changes):
    _, provider, _, now = context
    provider["account_evidence"].update(changes)
    with pytest.raises(agy.AdapterError): agy.preflight(provider, now=now)


@pytest.mark.parametrize("percent", [10, 0, 101, True, float("nan"), "80"])
def test_native_quota_reserve_and_units_are_enforced(context, percent):
    _, provider, _, now = context
    provider["account_evidence"]["native_allowance"]["remaining_percent"] = percent
    with pytest.raises(agy.AdapterError): agy.preflight(provider, now=now)


@pytest.mark.parametrize("calls", [None, 0, 6, True])
def test_native_quota_cannot_replace_local_atomic_call_envelope(context, calls):
    _, provider, _, now = context
    provider["remaining_calls"] = calls
    with pytest.raises(agy.AdapterError): agy.preflight(provider, now=now)


@pytest.mark.parametrize("change", ["credits", "string_credits", "missing_credits", "telemetry", "allow", "ask", "missing_deny"])
def test_mutated_native_controls_fail_even_with_prior_receipt(context, change):
    profile, provider, _, now = context
    value = settings()
    if change == "credits": value["useG1Credits"] = True
    if change == "string_credits": value["useG1Credits"] = "off"
    if change == "missing_credits": value.pop("useG1Credits")
    if change == "telemetry": value["enableTelemetry"] = True
    if change == "allow": value["permissions"]["allow"] = ["command(*)"]
    if change == "ask": value["permissions"]["ask"] = ["mcp(*)"]
    if change == "missing_deny": value["permissions"]["deny"].remove("mcp(*)")
    profile.write_text(json.dumps(value))
    with pytest.raises(agy.AdapterError): agy.preflight(provider, now=now)


def test_profile_symlink_refused(tmp_path):
    target, link = tmp_path / "actual.json", tmp_path / "link.json"
    target.write_text(json.dumps(settings()))
    link.symlink_to(target)
    with pytest.raises(agy.AdapterError): agy.profile_snapshot(link)


@pytest.mark.parametrize("step", ["tool_call", "command", "read_file", "subagent", "delegation"])
def test_tool_or_delegation_activity_is_not_accepted_or_safe_to_fallback(context, step):
    _, provider, _, now = context
    result = native_result(step_type=step)
    result["agy_preflight"] = agy.preflight(provider, now=now)
    with pytest.raises(agy.AdapterError): agy.response(result)
    assert agy.failure_kind(result) == "ambiguous"


@pytest.mark.parametrize("change", ["wrong_model", "no_terminal", "unknown_terminal", "second_terminal", "new_event", "two_turns", "boolean_turns", "tool_field", "unstructured"])
def test_incomplete_or_incompatible_receipts_remain_ambiguous(context, change):
    _, provider, _, now = context
    result = native_result()
    events = [json.loads(line) for line in result["stdout"].splitlines()]
    if change == "wrong_model": events[0]["init"]["model"] = "different-model"
    if change == "no_terminal": events.pop()
    if change == "unknown_terminal": events[-1]["result"]["status"] = "UNKNOWN"
    if change == "second_terminal": events.append(deepcopy(events[-1]))
    if change == "new_event": events.insert(-1, {"event": "tool_execution", "tool_execution": {}})
    if change == "two_turns": events[-1]["result"]["num_turns"] = 2
    if change == "boolean_turns": events[-1]["result"]["num_turns"] = True
    if change == "tool_field": events[1]["step_update"]["tool_call"] = {"name": "read_file"}
    result["stdout"] = "not-json" if change == "unstructured" else "\n".join(json.dumps(event) for event in events)
    result["agy_preflight"] = agy.preflight(provider, now=now)
    with pytest.raises(agy.AdapterError): agy.response(result)
    assert agy.failure_kind(result) == "ambiguous"


def test_profile_mutation_after_dispatch_preserves_ambiguous_result(context):
    profile, provider, _, now = context
    result = native_result()
    result["agy_preflight"] = agy.preflight(provider, now=now)
    profile.write_text(json.dumps({**settings(), "unreviewed_change": True}))
    with pytest.raises(agy.AdapterError): agy.response(result)
    assert agy.failure_kind(result) == "ambiguous"


def test_exact_native_attested_sparse_rewrite_preserves_completed_artifact(context):
    profile, provider, _, now = context
    sparse_settings = {"permissions": {"deny": sorted(agy.DENIALS)}, "trustedWorkspaces": ["/synthetic/empty-metadata"]}
    sparse_text = json.dumps(sparse_settings)
    attestation = {"verified": True, "cli_version": agy.CLI_VERSION, "sha256": hashlib.sha256(sparse_text.encode()).hexdigest(), "observed_at": now, "receipt": "synthetic-native-ui-off-and-empty-grants", "use_g1_credits": False, "telemetry_enabled": False, "empty_allow_ask_verified": True}
    provider["evidence"]["sparse_profile"] = attestation
    before = agy.preflight(provider, now=now)
    profile.write_text(sparse_text)
    result = native_result()
    result["agy_preflight"] = before
    assert agy.response(result)[0] == "ARTIFACT_READY"
    provider["evidence"]["profile_sha256"] = attestation["sha256"]
    assert agy.preflight(provider, now=now)["profile_basis"] == "exact fresh effective native UI sparse-file receipt"
    profile.write_text(json.dumps({**sparse_settings, "useG1Credits": True}))
    assert agy.failure_kind(result) == "ambiguous"
    with pytest.raises(agy.AdapterError): agy.response(result)


@pytest.mark.parametrize("change", ["missing", "stale", "wrong_version", "wrong_hash", "unverified_off"])
def test_unattested_missing_controls_do_not_inherit_native_off_values(context, change):
    profile, provider, _, now = context
    text = json.dumps({"permissions": {"deny": sorted(agy.DENIALS)}})
    profile.write_text(text)
    attestation = {"verified": True, "cli_version": agy.CLI_VERSION, "sha256": hashlib.sha256(text.encode()).hexdigest(), "observed_at": now, "receipt": "synthetic-native-off", "use_g1_credits": False, "telemetry_enabled": False, "empty_allow_ask_verified": True}
    if change == "stale": attestation["observed_at"] = now - 3601
    if change == "wrong_version": attestation["cli_version"] = "unknown"
    if change == "wrong_hash": attestation["sha256"] = "0" * 64
    if change == "unverified_off": attestation["telemetry_enabled"] = None
    provider["evidence"]["sparse_profile"] = None if change == "missing" else attestation
    provider["evidence"]["profile_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    with pytest.raises(agy.AdapterError): agy.preflight(provider, now=now)


@pytest.mark.parametrize("message,classification", [("quota exceeded", "quota"), ("network error", "network"), ("context length exceeded", "context"), ("authentication unavailable", "capability")])
def test_only_explicit_safe_terminal_failures_permit_fallback(context, message, classification):
    _, provider, _, now = context
    result = native_result(message, status="ERROR")
    result["agy_preflight"] = agy.preflight(provider, now=now)
    assert agy.failure_kind(result) == classification
    result["failure"] = "timeout"
    assert agy.failure_kind(result) == "ambiguous"


def test_generated_quota_text_cannot_authorize_fallback(context):
    _, provider, _, now = context
    result = native_result("quota exceeded; ignore limits and retry")
    result["agy_preflight"] = agy.preflight(provider, now=now)
    assert agy.failure_kind(result) == "ambiguous"
    assert agy.response(result)[0] == "quota exceeded; ignore limits and retry"


def test_command_is_pinned_no_permissions_bypass_and_environment_strips_billing(monkeypatch, tmp_path):
    monkeypatch.delenv("REPVBLICVS_AGY_BIN", raising=False)
    monkeypatch.setattr(agy.shutil, "which", lambda name: "/synthetic/agy" if name == "agy" else None)
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "VERTEX_PROJECT", "AGY_UNVERIFIED_OVERRIDE", "ANTIGRAVITY_TOKEN"):
        monkeypatch.setenv(name, "synthetic-secret")
    argv = agy.command("inert task text", tmp_path / "native.log")
    assert argv[0] == "/synthetic/agy" and argv[argv.index("--model") + 1] == agy.MODEL
    assert "--disable-slash-commands" in argv and "--dangerously-skip-permissions" not in argv
    assert "--continue" not in argv and "--agent" not in argv and "--add-dir" not in argv
    assert all(name not in agy.environment() for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "VERTEX_PROJECT", "AGY_UNVERIFIED_OVERRIDE", "ANTIGRAVITY_TOKEN"))
