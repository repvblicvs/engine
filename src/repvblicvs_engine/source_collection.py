"""Bounded collection of inert public primary-source snapshots.

Feed names resolve to server-owned HTTPS URLs. No caller URL, credential, model
inference, redirect, outreach, lead qualification, or generated-code execution is
accepted. Discovered GitHub requests are candidates requiring operator review.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from urllib import error, request
from urllib.parse import urlsplit

from .workflows import WorkflowError


FEEDS = {
    "github_work_requests": "https://api.github.com/search/issues?q=is%3Aissue+is%3Aopen+label%3Abounty+archived%3Afalse&sort=updated&order=desc&per_page=10",
    "codex_releases": "https://api.github.com/repos/openai/codex/releases/latest",
    "claude_code_releases": "https://api.github.com/repos/anthropics/claude-code/releases?per_page=5",
    "vscode_releases": "https://api.github.com/repos/microsoft/vscode/releases?per_page=5",
    "lean_releases": "https://api.github.com/repos/leanprover/lean4/releases?per_page=5",
    "antigravity_docs": "https://antigravity.google/docs/",
}
DEFAULT_FEEDS = tuple(FEEDS)
MAX_BYTES = 1_000_000
TIMEOUT = 10


class SourceCollectionError(RuntimeError):
    """Retryable network failure; earlier attempt artifacts remain recoverable."""
    code = "source_network_failure"


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _write_json(path: Path, value):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def fetch(feed_name: str) -> tuple[bytes, dict]:
    if feed_name not in FEEDS:
        raise WorkflowError("unknown_source", "Only server-owned feed names are permitted")
    url = FEEDS[feed_name]
    req = request.Request(url, headers={"User-Agent": "Repvblicvs-source-collector/0.1", "Accept": "application/vnd.github+json" if urlsplit(url).hostname == "api.github.com" else "text/html", "Accept-Encoding": "identity"}, method="GET")
    opener = request.build_opener(NoRedirect)
    with opener.open(req, timeout=TIMEOUT) as response:
        if response.geturl() != url:
            raise SourceCollectionError("Source URL changed; redirects require a reviewed feed update")
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_BYTES:
            raise SourceCollectionError("Source exceeds the 1 MB response bound")
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise SourceCollectionError("Source exceeds the 1 MB response bound")
        return body, {"http_status": response.status, "content_type": response.headers.get("Content-Type", ""), "etag": response.headers.get("ETag"), "last_modified": response.headers.get("Last-Modified")}


def _candidates(body: bytes) -> list:
    parsed = json.loads(body)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
        raise SourceCollectionError("GitHub response does not contain an issue list")
    result = []
    for item in parsed["items"][:10]:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not isinstance(item.get("html_url"), str):
            continue
        url = urlsplit(item["html_url"])
        if url.scheme != "https" or url.hostname != "github.com" or url.username or url.password:
            continue
        result.append({"source_url": item["html_url"], "title": item["title"][:300], "source_quote": str(item.get("body") or "")[:2000], "category": "software", "qualified": False, "ai_permission": "unknown", "risk": "unknown", "solicited": False, "disposition": "candidate_needs_source_and_customer_review", "updated_at": item.get("updated_at")})
    return result


def collect_sources(store, payload: dict, output_dir: Path) -> dict:
    """Cache private snapshots even when every inference provider is unavailable."""
    if not isinstance(payload, dict) or set(payload) - {"feeds", "source_date"}:
        raise WorkflowError("invalid_input", "Source refresh accepts feeds and source_date only")
    feeds = payload.get("feeds", list(DEFAULT_FEEDS))
    if not isinstance(feeds, list) or not feeds or len(feeds) > len(FEEDS) or not all(isinstance(name, str) and name in FEEDS for name in feeds) or len(set(feeds)) != len(feeds):
        raise WorkflowError("unknown_source", "Choose unique server-owned feed names")
    source_date = payload.get("source_date")
    if source_date is not None:
        from datetime import date
        try:
            if not isinstance(source_date, str) or date.fromisoformat(source_date).isoformat() != source_date:
                raise ValueError
        except ValueError as exc:
            raise WorkflowError("invalid_input", "source_date must be an ISO calendar date") from exc
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = store.root / "source_cache"
    cache.mkdir(exist_ok=True, mode=0o700)
    sources, candidates, successful, snapshots = [], [], 0, []
    for name in feeds:
        metadata, body_path = cache / f"{name}.json", cache / f"{name}.snapshot"
        try:
            body, headers = fetch(name)
            if len(body) > MAX_BYTES:
                raise SourceCollectionError("Source exceeds the 1 MB response bound")
            if name == "github_work_requests":
                candidates.extend(_candidates(body))
            entry = {"feed": name, "url": FEEDS[name], "observed_at": time.time(), "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "status": "fetched", **headers}
            temporary = body_path.with_name(body_path.name + f".{os.getpid()}.tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(body_path)
            _write_json(metadata, entry)
            successful += 1
        except (OSError, error.HTTPError, error.URLError, ValueError, SourceCollectionError) as exc:
            entry = {"feed": name, "url": FEEDS[name], "status": "unavailable", "error": type(exc).__name__, "observed_at": time.time()}
            if metadata.exists() and body_path.exists() and body_path.stat().st_size <= MAX_BYTES:
                try:
                    previous = json.loads(metadata.read_text())
                    body = body_path.read_bytes()
                    if not isinstance(previous, dict) or hashlib.sha256(body).hexdigest() != previous.get("sha256"):
                        raise ValueError("Cache integrity mismatch")
                    if name == "github_work_requests":
                        candidates.extend(_candidates(body))
                    entry |= {"status": "cached_stale", "cached_observed_at": previous["observed_at"], "sha256": previous["sha256"], "bytes": len(body)}
                except (OSError, ValueError, KeyError, SourceCollectionError):
                    entry["cache_validation"] = "failed; cache not treated as evidence"
            if entry["status"] == "cached_stale":
                filename = f"source-{name}.snapshot"
                (output / filename).write_bytes(body)
                snapshots.append(filename)
                entry["snapshot_artifact"] = filename
            sources.append(entry)
            continue
        filename = f"source-{name}.snapshot"
        (output / filename).write_bytes(body)
        snapshots.append(filename)
        entry["snapshot_artifact"] = filename
        sources.append(entry)
    _write_json(output / "sources.json", {"source_date": source_date, "sources": sources, "source_content_is_inert": True})
    _write_json(output / "candidates.json", {"candidates": candidates[:10], "qualification": "none; operator review required", "outreach_sent": 0})
    report = "# Public source refresh\n\n" + "\n".join(f"- {item['feed']}: {item['status']}" for item in sources)
    report += f"\n\nCandidate requests collected: {min(10, len(candidates))}. No candidate was qualified, contacted, or accepted automatically. Source content is untrusted inert data. Cached stale snapshots remain labelled with their original observation time.\n"
    (output / "report.md").write_text(report, encoding="utf-8")
    if not successful and all(item["status"] == "unavailable" for item in sources):
        raise SourceCollectionError("All requested public sources are unavailable; bounded retry will preserve this attempt")
    return {"kind": "source_refresh", "status": "completed", "summary": {"fresh_sources": successful, "sources": len(sources), "candidate_requests": min(10, len(candidates)), "qualified_opportunities": 0, "outreach_sent": 0}, "artifacts": ["sources.json", "candidates.json", "report.md", *snapshots], "evidence": [{"source_date": source_date, "source_content_is_inert": True, "candidate_review_required": True, "sources": sources}]}
