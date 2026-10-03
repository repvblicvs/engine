"""Fresh, non-inference Codex allowance checks and conservative owner reserves.

The app-server handshake follows the audited Tessera allowance reader's protocol.
This is an independently implemented adapter; original Tessera sources remain
untouched. No reset-credit, purchase, thread, or inference operation is issued.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time

from .store import encode

CREDIT_POLICY_KEY = "codex_credit_policy"
CREDIT_USAGE_KEY = "codex_credit_usage"


def _finite_number(value, *, string: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str) if string else (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _validated_credit_policy(policy: dict | None) -> dict | None:
    """Accept only the trusted owner's existing-balance authorization, in credits.

    No task payload or provider entitlement can grant this permission. The caller
    must read it from private Store.meta, set by an explicitly authorized owner.
    """
    if not isinstance(policy, dict) or policy.get("authorized") is not True or policy.get("scope") != "existing_codex_balance" or policy.get("purchases_allowed") is not False or policy.get("refill_allowed") is not False:
        return None
    initial = _finite_number(policy.get("initial_balance"))
    reserve = _finite_number(policy.get("reserve_credits"))
    maximum = _finite_number(policy.get("max_balance_to_use"))
    receipt = policy.get("authorization_receipt")
    if initial is None or reserve is None or maximum is None or reserve < 50 or maximum <= 0 or initial <= reserve or maximum > initial - reserve or not isinstance(receipt, str) or not receipt.strip() or len(receipt) > 200:
        return None
    return {"authorized": True, "scope": "existing_codex_balance", "initial_balance": initial, "reserve_credits": reserve, "max_balance_to_use": maximum, "authorization_receipt": receipt, "purchases_allowed": False, "refill_allowed": False}


def safe_environment() -> dict:
    """Keep OAuth home context, remove paid-provider keys and override switches."""
    blocked = ("ANTHROPIC_", "OPENAI_", "AZURE_", "AWS_", "GOOGLE_", "GEMINI_", "VERTEX_", "CLAUDE_CODE_USE_", "CODEX_API_")
    return {key: value for key, value in os.environ.items() if not key.startswith(blocked) and key not in {"API_KEY", "API_TOKEN", "CLAUDE_API_KEY", "CODEX_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"}}


def codex_binary() -> str:
    configured = os.environ.get("REPVBLICVS_CODEX_BIN")
    discovered = configured or shutil.which("codex")
    if discovered:
        return discovered
    for application in ("Codex", "ChatGPT"):
        candidate = Path(f"/Applications/{application}.app/Contents/Resources/codex")
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("Codex executable is unavailable")


def read_live_limits(timeout: float = 12) -> dict:
    """Read only initialize + account/rateLimits/read through Codex app-server."""
    process = subprocess.Popen([codex_binary(), "app-server", "--listen", "stdio://"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=safe_environment())
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline, buffered = time.monotonic() + timeout, bytearray()
    def request(number, method, params):
        process.stdin.write((json.dumps({"id": number, "method": method, "params": params}) + "\n").encode())
        process.stdin.flush()
        while time.monotonic() < deadline:
            while b"\n" in buffered:
                end = buffered.index(b"\n")
                line = bytes(buffered[:end])
                del buffered[:end + 1]
                try:
                    response = json.loads(line)
                except ValueError:
                    continue
                if isinstance(response, dict) and response.get("id") == number:
                    if "error" in response or not isinstance(response.get("result"), dict):
                        raise RuntimeError("Codex allowance read was refused")
                    return response["result"]
            if not selector.select(max(0, deadline - time.monotonic())):
                break
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("Codex allowance reader exited without limits")
            buffered.extend(chunk)
            if len(buffered) > 2_000_000:
                raise RuntimeError("Codex allowance response exceeds 2 MB")
        raise RuntimeError("Codex allowance read timed out; no inference was started")
    try:
        request(1, "initialize", {"clientInfo": {"name": "repvblicvs-allowance", "version": "0.1.0"}})
        return request(2, "account/rateLimits/read", {})
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()


def normalize(raw: dict, now: float | None = None) -> dict:
    """Preserve quota versus hard-block reasons and credit units, without IDs.

    Balance is a Codex credit quantity, never a dollar price or token conversion.
    The legacy/top-level layouts are accepted; malformed booleans stay unknown.
    """
    buckets = raw.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = raw.get("rateLimits")
        buckets = {legacy.get("limitId") or "codex": legacy} if isinstance(legacy, dict) else {}
    result = {}
    for name, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        windows = []
        for slot in ("primary", "secondary"):
            window = bucket.get(slot)
            if not isinstance(window, dict):
                continue
            used, duration, reset = window.get("usedPercent"), window.get("windowDurationMins"), window.get("resetsAt")
            if any(_finite_number(value) is None for value in (used, duration, reset)) or not 0 <= used <= 100 or duration <= 0 or reset <= 0:
                continue
            windows.append({"slot": slot, "used_percent": used, "remaining_percent": max(0, min(100, 100 - used)), "duration_minutes": duration, "resets_at": reset})
        def field(key):
            return bucket[key] if key in bucket else raw.get(key)
        reached = field("rateLimitReachedType")
        spend = field("spendControlReached")
        ordinary = field("ordinaryUsageAllowed")
        credits = field("credits")
        credits = credits if isinstance(credits, dict) else {}
        balance = _finite_number(credits.get("balance"), string=True)
        result[str(name)] = {
            "windows": windows,
            "rate_limit_reached_type": reached if isinstance(reached, str) else None,
            "spend_control_reached": spend if isinstance(spend, bool) else None,
            "ordinary_usage_allowed": ordinary if isinstance(ordinary, bool) else None,
            "provider_blocked": reached not in (None, "rate_limit_reached") or (spend is not None and spend is not False) or (ordinary is not None and not isinstance(ordinary, bool)),
            "credits": {"has_credits": credits.get("hasCredits") if isinstance(credits.get("hasCredits"), bool) else None, "balance": balance if balance is not None and balance >= 0 else None, "unit": "codex_credits"},
        }
    return {"source": "Codex account/rateLimits/read; no inference", "observed_at": time.time() if now is None else now, "buckets": result}


def routing_decision(snapshot: dict, limit_id: str = "codex", now: float | None = None, *, credit_policy: dict | None = None, observed_depletion_credits: float = 0) -> dict:
    """Prefer included usage; permit only the original authorized credit ceiling.

    Unknown provider blocks, stale evidence and malformed windows cannot be
    bypassed with a balance. Credit reserve enforcement is an observed preflight
    gate: no verified CLI option bounds actual credit debit during a final call.
    Bounded packets/time/output reduce exposure, but are not a provider spend cap.
    """
    now = time.time() if now is None else now
    observed = snapshot.get("observed_at", 0)
    def decision(allowed, reason, billing_mode="unavailable", **extra):
        return {"allowed": allowed, "reason": reason, "billing_mode": billing_mode, **extra}
    if _finite_number(observed) is None or not 0 <= now - observed <= 30:
        return decision(False, "Allowance evidence is stale or future-dated")
    bucket = snapshot.get("buckets", {}).get(limit_id, {})
    windows = bucket.get("windows", [])
    if not windows or bucket.get("provider_blocked"):
        return decision(False, "Allowance unavailable or blocked by provider")
    durations = {window["duration_minutes"] for window in windows}
    if not {300, 10080}.issubset(durations):
        return decision(False, "Five-hour and weekly windows are both required")
    reserve_reason = None
    for window in windows:
        reserve = 10 if window["duration_minutes"] == 300 else 20
        if window["resets_at"] <= now:
            return decision(False, "Allowance window reset requires a fresh read")
        if window["remaining_percent"] <= reserve:
            reserve_reason = f"{reserve}% owner reserve retained for {window['duration_minutes']}-minute window"
    ordinary_refused = bucket.get("ordinary_usage_allowed") is False or bucket.get("rate_limit_reached_type") == "rate_limit_reached"
    if not ordinary_refused and reserve_reason is None:
        return decision(True, "Fresh included allowance exceeds owner reserves", "included")
    reason = reserve_reason or "Ordinary included quota is exhausted"
    policy = _validated_credit_policy(credit_policy)
    if policy is None:
        return decision(False, reason + "; no trusted existing-credit authorization")
    # The CLI has no proven force-credit switch. Local included-reserve pressure
    # alone cannot select prepaid billing while ordinary usage is still allowed.
    if bucket.get("spend_control_reached") is not False or bucket.get("rate_limit_reached_type") != "rate_limit_reached" or bucket.get("ordinary_usage_allowed") is True:
        return decision(False, "Existing-credit fallback requires a verified ordinary quota refusal and no spending-control block")
    credits = bucket.get("credits", {})
    balance = _finite_number(credits.get("balance"))
    consumed = _finite_number(observed_depletion_credits)
    if credits.get("has_credits") is not True or balance is None or consumed is None or consumed < 0 or balance <= policy["reserve_credits"]:
        return decision(False, "Verified existing credits must exceed the owner's credit reserve")
    remaining = max(0, policy["max_balance_to_use"] - consumed)
    if remaining <= 0:
        return decision(False, "Original authorized existing-credit ceiling is exhausted; refills do not expand it")
    return decision(True, reason + "; using owner-authorized existing Codex credits", "authorized_existing_credits", credit_balance=balance, credit_unit="codex_credits", credit_reserve=policy["reserve_credits"], original_credit_ceiling=policy["max_balance_to_use"], observed_depletion_credits=consumed, remaining_authorized_credits=min(balance - policy["reserve_credits"], remaining), authorization_receipt=policy["authorization_receipt"], reserve_enforcement="observed preflight/postflight; no provider-side per-call credit cap")


def dispatch_decision(snapshot: dict, limit_id: str = "codex", now: float | None = None, *, credit_policy: dict | None = None, observed_depletion_credits: float = 0) -> tuple[bool, str]:
    """Compatibility wrapper returning allowed/reason for the same routing gate."""
    decision = routing_decision(snapshot, limit_id, now, credit_policy=credit_policy, observed_depletion_credits=observed_depletion_credits)
    return decision["allowed"], decision["reason"]


def observe_credit_balance(store, snapshot: dict, limit_id: str = "codex") -> tuple[dict | None, dict | None]:
    """Atomically retain cumulative *shared-account* depletion; never model cost.

    Increases/refills do not replenish the original authorization. Observation
    state is independent of the policy and survives service/account refreshes.
    Concurrent usage/refills may occur between reads: these are observed deltas,
    not an attribution or a complete transaction ledger from the provider.
    """
    with store.connection(write=True) as db:
        policy_row = db.execute("SELECT value FROM meta WHERE key=?", (CREDIT_POLICY_KEY,)).fetchone()
        try:
            policy = _validated_credit_policy(json.loads(policy_row[0])) if policy_row else None
        except (ValueError, TypeError):
            policy = None
        if policy is None:
            return None, None
        observed = snapshot.get("observed_at")
        if _finite_number(observed) is None or not 0 <= time.time() - observed <= 30:
            raise RuntimeError("Credit balance observation is stale or future-dated")
        balance = snapshot.get("buckets", {}).get(limit_id, {}).get("credits", {}).get("balance")
        if _finite_number(balance) is None:
            return policy, None
        fingerprint = hashlib.sha256(encode(policy).encode()).hexdigest()
        usage_row = db.execute("SELECT value FROM meta WHERE key=?", (CREDIT_USAGE_KEY,)).fetchone()
        try:
            previous = json.loads(usage_row[0]) if usage_row else None
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Retained credit observation ledger is malformed; reconciliation required") from exc
        if usage_row is not None:
            numeric_keys = ("last_observed_at", "last_observed_balance", "observed_depletion_credits", "remaining_original_authorized_credits")
            if not isinstance(previous, dict) or any(_finite_number(previous.get(key)) is None or previous[key] < 0 for key in numeric_keys) or previous.get("unit") != "codex_credits" or previous.get("attribution") != "shared_account_unattributed" or previous.get("authorization_receipt") != policy["authorization_receipt"] or not isinstance(previous.get("policy_hash"), str):
                raise RuntimeError("Retained credit observation ledger is malformed; reconciliation required")
            if previous["policy_hash"] != fingerprint:
                raise RuntimeError("Existing-credit authorization changed; explicit ledger reconciliation required")
            if previous["last_observed_at"] > time.time() or previous["remaining_original_authorized_credits"] != max(0, policy["max_balance_to_use"] - previous["observed_depletion_credits"]) or previous["observed_depletion_credits"] < max(0, policy["initial_balance"] - previous["last_observed_balance"]):
                raise RuntimeError("Retained credit observation ledger is inconsistent; reconciliation required")
        if previous and (observed < previous["last_observed_at"] or (observed == previous["last_observed_at"] and balance != previous["last_observed_balance"])):
            raise RuntimeError("Credit balance observation precedes/conflicts with the retained account observation")
        prior_balance = previous["last_observed_balance"] if previous else policy["initial_balance"]
        delta = max(0, prior_balance - balance)
        consumed = (previous["observed_depletion_credits"] if previous else 0) + delta
        usage = {"policy_hash": fingerprint, "authorization_receipt": policy["authorization_receipt"], "last_observed_at": observed, "last_observed_balance": balance, "observed_depletion_credits": consumed, "remaining_original_authorized_credits": max(0, policy["max_balance_to_use"] - consumed), "unit": "codex_credits", "attribution": "shared_account_unattributed"}
        db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (CREDIT_USAGE_KEY, encode(usage)))
        if previous is None or balance != prior_balance:
            store._event(db, "codex_credit_balance_observed", balance_credits=balance, observed_depletion_credits=delta, cumulative_observed_depletion_credits=consumed, remaining_original_authorized_credits=usage["remaining_original_authorized_credits"], observed_increase_credits=max(0, balance - prior_balance), unit="codex_credits", attribution="shared_account_unattributed", authorization_receipt=policy["authorization_receipt"])
        return policy, usage


def refresh_sol(store, router, provider: dict, *, raise_on_unavailable: bool = True) -> dict:
    snapshot = normalize(read_live_limits())
    directory = store.root / "usage"
    directory.mkdir(exist_ok=True, mode=0o700)
    target = directory / "codex.json"
    temporary = directory / f".codex-{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(snapshot, stream, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)
    limit_id = provider.get("evidence", {}).get("limit_id", "codex")
    try:
        policy, usage = observe_credit_balance(store, snapshot, limit_id)
        decision = routing_decision(snapshot, limit_id, credit_policy=policy, observed_depletion_credits=usage["observed_depletion_credits"] if usage else 0)
        if decision["billing_mode"] == "authorized_existing_credits" and usage is None:
            decision = {"allowed": False, "reason": "Existing-credit observation ledger unavailable", "billing_mode": "unavailable"}
    except RuntimeError as exc:
        decision = {"allowed": False, "reason": str(exc), "billing_mode": "unavailable"}
        usage = None
    snapshot["dispatch"] = decision
    snapshot["credit_observation"] = usage
    reset = min((window["resets_at"] for window in snapshot["buckets"].get(limit_id, {}).get("windows", [])), default=None)
    router.configure_account(provider["account_id"], evidence={"source": snapshot["source"], "observed_at": snapshot["observed_at"], "reserve_five_hour_percent": 10, "reserve_week_percent": 20, "billing_mode": decision["billing_mode"], "dispatch_decision": decision, "credit_observation": usage, "no_new_cash_charge_authorized": True}, verified_until=time.time() + 30, remaining_calls=None, status="verified" if decision["allowed"] else "exhausted", reset_at=reset)
    if not decision["allowed"] and raise_on_unavailable:
        raise RuntimeError(decision["reason"])
    return snapshot
