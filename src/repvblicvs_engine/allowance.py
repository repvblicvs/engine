"""Fresh, non-inference Codex allowance checks and conservative owner reserves.

The app-server handshake follows the audited Tessera allowance reader's protocol.
This is an independently implemented adapter; original Tessera sources remain
untouched. No reset-credit, purchase, thread, or inference operation is issued.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time


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
    """Store allowance windows without account IDs, credentials, or credit IDs."""
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
            if any(isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) for value in (used, duration, reset)):
                continue
            windows.append({"slot": slot, "used_percent": used, "remaining_percent": max(0, min(100, 100 - used)), "duration_minutes": duration, "resets_at": reset})
        result[str(name)] = {"windows": windows, "provider_blocked": bool(bucket.get("rateLimitReachedType") or bucket.get("spendControlReached"))}
    return {"source": "Codex account/rateLimits/read; no inference", "observed_at": time.time() if now is None else now, "buckets": result}


def dispatch_decision(snapshot: dict, limit_id: str = "codex", now: float | None = None) -> tuple[bool, str]:
    """Require a fresh check and retain 10% five-hour / 20% weekly allowance."""
    now = time.time() if now is None else now
    observed = snapshot.get("observed_at", 0)
    if not isinstance(observed, (int, float)) or not math.isfinite(observed) or not 0 <= now - observed <= 30:
        return False, "Allowance evidence is stale or future-dated"
    bucket = snapshot.get("buckets", {}).get(limit_id, {})
    windows = bucket.get("windows", [])
    if not windows or bucket.get("provider_blocked"):
        return False, "Allowance unavailable or blocked by provider"
    durations = {window["duration_minutes"] for window in windows}
    if not {300, 10080}.issubset(durations):
        return False, "Five-hour and weekly windows are both required"
    for window in windows:
        reserve = 10 if window["duration_minutes"] == 300 else 20
        if window["resets_at"] <= now:
            return False, "Allowance window reset requires a fresh read"
        if window["remaining_percent"] <= reserve:
            return False, f"{reserve}% owner reserve retained for {window['duration_minutes']}-minute window"
    return True, "Fresh included allowance exceeds owner reserves"


def refresh_sol(store, router, provider: dict) -> dict:
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
    allowed, reason = dispatch_decision(snapshot, limit_id)
    reset = min((window["resets_at"] for window in snapshot["buckets"].get(limit_id, {}).get("windows", [])), default=None)
    router.configure_account(provider["account_id"], evidence={"source": snapshot["source"], "observed_at": snapshot["observed_at"], "reserve_five_hour_percent": 10, "reserve_week_percent": 20}, verified_until=time.time() + 30, remaining_calls=None, status="verified" if allowed else "exhausted", reset_at=reset)
    if not allowed:
        raise RuntimeError(reason)
    return snapshot
