"""Verified Antigravity CLI artifact worker, using existing cached Google OAuth.

Native tools remain advertised. Namespace denies and a reviewed effective native
profile are required; an accepted response additionally has zero tool/delegation
events. ``--mode plan`` has no security effect with disabled slash expansion.
No legacy Gemini login, API key, new agreement, or credit purchase is performed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import time

from .allowance import safe_environment

MODEL = "gemini-3.1-pro-low"
CLI_VERSION = "1.2.16"
MAX_PACKET = 20000
MAX_SECONDS = 90
MAX_OUTPUT = 65536
MAX_EVIDENCE_AGE = 3600
DENIALS = frozenset(f"{name}(*)" for name in ("read_file", "write_file", "read_url", "execute_url", "command", "mcp"))


class AdapterError(ValueError):
    pass


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _receipt(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2000


def _fresh(value, now):
    value = _number(value)
    return value is not None and 0 <= now - value <= MAX_EVIDENCE_AGE


def _verified_sparse(value, now=None):
    """Accept one observed sparse rewrite, never generic missing-setting defaults."""
    now = time.time() if now is None else now
    return isinstance(value, dict) and value.get("verified") is True and value.get("cli_version") == CLI_VERSION and isinstance(value.get("sha256"), str) and len(value["sha256"]) == 64 and _fresh(value.get("observed_at"), now) and _receipt(value.get("receipt")) and value.get("use_g1_credits") is False and value.get("telemetry_enabled") is False and value.get("empty_allow_ask_verified") is True


def profile_snapshot(path: Path | None = None, *, sparse=None) -> dict:
    """Read controls without following links or exporting private native values."""
    path = path or Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 200000:
                raise AdapterError("Native profile ownership, type or size is invalid")
            raw = stream.read(200001)
        settings = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise AdapterError("Current native profile cannot be verified") from exc
    permissions = settings.get("permissions") if isinstance(settings, dict) else None
    digest = hashlib.sha256(raw).hexdigest()
    known_sparse = _verified_sparse(sparse) and sparse["sha256"] == digest
    if not isinstance(permissions, dict) or settings.get("useG1Credits", False if known_sparse else None) is not False or settings.get("enableTelemetry", False if known_sparse else None) is not False:
        raise AdapterError("Explicit native credits-off, telemetry-off and permission controls are required")
    deny = permissions.get("deny")
    if permissions.get("allow", [] if known_sparse else None) != [] or permissions.get("ask", [] if known_sparse else None) != [] or not isinstance(deny, list) or any(not isinstance(item, str) for item in deny) or not DENIALS.issubset(deny):
        raise AdapterError("Native profile must deny all executable namespaces with empty allow and ask grants")
    return {"profile_sha256": digest, "use_g1_credits": False, "telemetry_enabled": False, "namespace_denials": sorted(DENIALS), "ignored_namespace_rules": ["unsandboxed(*)"] if "unsandboxed(*)" in deny else [], "tool_inventory_removed": False, "profile_basis": "exact fresh effective native UI sparse-file receipt" if known_sparse else "explicit settings plus fresh effective native UI receipt"}


def _same_profile(before):
    sparse = before.get("verified_sparse_profile")
    snapshot = profile_snapshot(sparse=sparse)
    hashes = {before.get("profile_sha256")}
    if _verified_sparse(sparse): hashes.add(sparse["sha256"])
    return snapshot["profile_sha256"] in hashes


def preflight(provider: dict, *, now: float | None = None) -> dict:
    """Require effective profile and native included-quota receipts, never guesses.

    The trusted operator records ``native_allowance`` under shared account
    evidence after an actual non-inference usage check. The bounded local call
    envelope is distinct from native quota and cannot substitute for its receipt.
    A fresh profile file alone does not prove effective native settings/onboarding.
    """
    now = time.time() if now is None else now
    evidence, account = provider.get("evidence", {}), provider.get("account_evidence", {})
    if not isinstance(evidence, dict) or not isinstance(account, dict):
        raise AdapterError("Verified native provider and account records are required")
    if provider.get("model") != MODEL or evidence.get("auth") != "google_cached_oauth" or evidence.get("cli_version") != CLI_VERSION:
        raise AdapterError("Verified exact Antigravity model, CLI and cached OAuth route are required")
    if evidence.get("profile_verified") is not True or not _fresh(evidence.get("profile_verified_at"), now) or not _receipt(evidence.get("profile_receipt")) or evidence.get("use_g1_credits") is not False:
        raise AdapterError("Fresh effective native profile and credits-off evidence are required")
    if account.get("billing_mode") != "included" or account.get("use_g1_credits") is not False or not _receipt(account.get("credits_off_receipt")):
        raise AdapterError("Native included allowance and disabled credit overage must be attested")
    quota = account.get("native_allowance")
    if not isinstance(quota, dict) or not isinstance(quota.get("models"), list) or MODEL not in quota["models"] or not _fresh(quota.get("observed_at"), now) or not _receipt(quota.get("receipt")):
        raise AdapterError("Fresh native usage evidence for the exact model pool is required")
    percent, reset = _number(quota.get("remaining_percent")), _number(quota.get("reset_at"))
    calls = provider.get("remaining_calls")
    if percent is None or not 10 < percent <= 100 or reset is None or reset <= now or isinstance(calls, bool) or not isinstance(calls, int) or not 1 <= calls <= 5:
        raise AdapterError("Native quota reserve or bounded local call envelope is unavailable")
    snapshot = profile_snapshot(sparse=evidence.get("sparse_profile"))
    if evidence.get("profile_sha256") != snapshot["profile_sha256"]:
        raise AdapterError("Native settings differ from the reviewed effective profile")
    return snapshot | {"billing_mode": "included", "included_allowance": True, "paid_charge_recorded": 0, "native_usage_receipt": quota["receipt"], "native_usage_observed_at": quota["observed_at"], "native_remaining_percent": percent, "native_reset_at": reset, "profile_receipt": evidence["profile_receipt"], "local_call_envelope": calls, "native_quota_is_call_count": False, "verified_sparse_profile": evidence.get("sparse_profile") if _verified_sparse(evidence.get("sparse_profile"), now) else None}


def command(prompt: str, log_path: Path) -> list[str]:
    """Fixed observed CLI contract; task data cannot select commands or settings."""
    binary = os.environ.get("REPVBLICVS_AGY_BIN") or shutil.which("agy")
    if not binary:
        raise AdapterError("Verified Antigravity CLI executable is unavailable")
    return [binary, "-p", prompt, "--model", MODEL, "--mode", "plan", "--effort", "low", "--disable-slash-commands", "--print-timeout", "90s", "--output-format", "stream-json", "--log-file", str(log_path)]


def environment() -> dict:
    """Keep existing native cached OAuth and strip provider/override environments."""
    return {key: value for key, value in safe_environment().items() if not key.startswith(("AGY_", "ANTIGRAVITY_"))}


def _events(result: dict) -> tuple[list[dict], dict]:
    if result.get("failure"):
        raise AdapterError("Native transport outcome is incomplete")
    try:
        events = [json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite stream value"))) for line in result.get("stdout", "").splitlines() if line.strip()]
    except (ValueError, TypeError) as exc:
        raise AdapterError("Malformed native event stream") from exc
    if not events or any(not isinstance(event, dict) for event in events):
        raise AdapterError("Native stream lacks structured events")
    initial = [event.get("init") for event in events if event.get("event") == "init"]
    terminal = [event.get("result") for event in events if event.get("event") == "result"]
    if len(initial) != 1 or not isinstance(initial[0], dict) or events[0].get("event") != "init" or initial[0].get("model") != MODEL or initial[0].get("permission_mode") != "request-review" or not isinstance(initial[0].get("tools"), list):
        raise AdapterError("Native exact model or effective permission mode differs")
    if len(terminal) != 1 or not isinstance(terminal[0], dict) or events[-1].get("event") != "result":
        raise AdapterError("Native stream lacks one final terminal result")
    for event in events:
        if event.get("event") not in {"init", "step_update", "result"}:
            raise AdapterError("Native stream contains tool, delegation or unknown activity")
        if set(event) - {"event", event["event"], "conversation_id"}:
            raise AdapterError("Native event contains unknown activity fields")
        if event["event"] == "step_update":
            step = event.get("step_update")
            allowed = {"conversation_id", "state", "step_index", "step_type", "text_delta", "usage", "duration_seconds"}
            if not isinstance(step, dict) or set(step) - allowed or step.get("step_type") not in {"user_input", "agent_response"} or step.get("state") not in {"ACTIVE", "DONE"}:
                raise AdapterError("Native step contains tool, delegation or unknown activity")
        if event["event"] == "result" and set(terminal[0]) - {"conversation_id", "duration_seconds", "num_turns", "response", "status", "usage", "model", "error"}:
            raise AdapterError("Native result contains unexpected executable activity")
    return events, terminal[0]


def response(result: dict) -> tuple[str, dict]:
    events, terminal = _events(result)
    if result.get("exit_code") != 0 or terminal.get("status") != "SUCCESS" or isinstance(terminal.get("num_turns"), bool) or terminal.get("num_turns") != 1 or terminal.get("model", MODEL) != MODEL or not isinstance(terminal.get("response"), str) or not terminal["response"].strip():
        raise AdapterError("Native result is not an exact-model single-turn SUCCESS")
    profile = result.get("agy_preflight")
    if not isinstance(profile, dict) or not _same_profile(profile):
        raise AdapterError("Native profile changed during the request; reconcile before retry")
    advertised = events[0]["init"]["tools"]
    return terminal["response"], {"provider": "gemini", "model": MODEL, "transport": "antigravity_cli", "cli_version": CLI_VERSION, "included_allowance": True, "paid_charge_recorded": 0, "usage": terminal.get("usage", {}), "native_stream_sha256": hashlib.sha256(result["stdout"].encode()).hexdigest(), "adapter": profile | {"terminal_status": "SUCCESS", "num_turns": 1, "tool_events_observed": 0, "delegation_events_observed": 0, "event_count": len(events), "tools_advertised": bool(advertised), "advertised_tool_count": len(advertised), "plan_mode_is_security_boundary": False}, "model_identity_basis": "exact pinned CLI argument and observed init model; terminal SUCCESS; no substitution"}


def failure_kind(result: dict) -> str:
    """Fallback only on an explicit safe terminal failure; unknown activity stops.

    Generated answer text, a partial stream, tool activity, or an identity/profile
    mismatch cannot manufacture permission to retry or switch providers.
    """
    try:
        _, terminal = _events(result)
        profile = result.get("agy_preflight")
        if not isinstance(profile, dict) or not _same_profile(profile) or terminal.get("model", MODEL) != MODEL or terminal.get("status") not in {"FAILURE", "FAILED", "ERROR"}:
            return "ambiguous"
    except AdapterError:
        return "ambiguous"
    error = (str(terminal.get("error", "")) + "\n" + str(terminal.get("response", ""))).lower()
    if any(word in error for word in ("quota exceeded", "rate limit", "usage limit", "resource_exhausted")): return "quota"
    if any(word in error for word in ("connection refused", "connection reset", "network error", "dns resolution", "failed to connect")): return "network"
    if any(word in error for word in ("context length", "context window", "too many tokens")): return "context"
    if any(word in error for word in ("model unavailable", "unsupported model", "not authorized", "authentication", "terms of service", "onboarding")): return "capability"
    return "ambiguous"
