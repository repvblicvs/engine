"""MCP stdio bridge over the same private store used by CLI and VS Code.

Protocol traffic is newline-delimited JSON-RPC; diagnostics never use stdout.
No credentials, unrestricted shell execution, or payment dispatch tools are exposed.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import stat
import sys
from .providers import Router
from .store import Store
from . import __version__


def schema(properties=None, required=None):
    result = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        result["required"] = required
    return result


TOOLS = [
    {"name": "engine_submit", "description": "Submit a durable task; reuse request_id to avoid duplicate execution", "inputSchema": schema({"spec": {"type": "object"}, "request_id": {"type": "string"}}, ["spec", "request_id"])},
    {"name": "engine_status", "description": "Inspect shared engine status", "inputSchema": schema()},
    {"name": "engine_tasks", "description": "Inspect tasks or one task", "inputSchema": schema({"task_id": {"type": "string"}})},
    {"name": "engine_control", "description": "Pause, resume, or stop new claims; active work drains safely", "inputSchema": schema({"mode": {"type": "string", "enum": ["running", "paused", "stopped"]}}, ["mode"])},
    {"name": "engine_priority", "description": "Change a task's scheduling priority", "inputSchema": schema({"task_id": {"type": "string"}, "priority": {"type": "integer", "minimum": -1000, "maximum": 1000}}, ["task_id", "priority"])},
    {"name": "engine_events", "description": "Read the shared event log after an event ID", "inputSchema": schema({"after_id": {"type": "integer", "minimum": 0}})},
    {"name": "engine_artifacts", "description": "List task artifacts, using relative artifact paths", "inputSchema": schema({"task_id": {"type": "string"}})},
    {"name": "engine_read_artifact", "description": "Read a UTF-8 artifact up to 1 MB within private artifact storage", "inputSchema": schema({"path": {"type": "string"}}, ["path"])},
    {"name": "engine_providers", "description": "Inspect provider and shared allowance readiness; unverified routes remain unavailable", "inputSchema": schema()},
    {"name": "engine_reserve_provider", "description": "Reserve verified included allowance or capped existing Fable credits; this does not invoke a model", "inputSchema": schema({"request_id": {"type": "string"}, "ceiling": {"type": "number", "minimum": 0, "maximum": 5}, "preferred": {"type": "string", "enum": ["fable", "opus", "sol", "gemini", "copilot"]}}, ["request_id"])},
    {"name": "engine_operator_claim", "description": "Trusted frontend claims an operator_review obligation with explicit identity and idempotent receipt; no external action is sent", "inputSchema": schema({"operator_id": {"type": "string"}, "receipt_id": {"type": "string"}, "task_id": {"type": "string"}, "lease_seconds": {"type": "number", "minimum": 1, "maximum": 3600}}, ["operator_id", "receipt_id"])},
    {"name": "engine_operator_complete", "description": "Commit bounded evidence and an immutable hashed receipt for a claimed operator review", "inputSchema": schema({"operator_id": {"type": "string"}, "receipt_id": {"type": "string"}, "lease_token": {"type": "string"}, "evidence": {"type": "object"}, "result": {"type": "object"}}, ["operator_id", "receipt_id", "lease_token", "evidence", "result"])},
    {"name": "engine_operator_defer", "description": "Preserve an unfinished operator obligation and record its explicit blocker", "inputSchema": schema({"operator_id": {"type": "string"}, "receipt_id": {"type": "string"}, "lease_token": {"type": "string"}, "reason": {"type": "string"}}, ["operator_id", "receipt_id", "lease_token", "reason"])},
    {"name": "engine_operator_renew", "description": "Extend a still-owned operator lease while verified connector work continues", "inputSchema": schema({"operator_id": {"type": "string"}, "receipt_id": {"type": "string"}, "lease_token": {"type": "string"}, "lease_seconds": {"type": "number", "minimum": 1, "maximum": 3600}}, ["operator_id", "receipt_id", "lease_token"])},
]


class Server:
    def __init__(self, store: Store):
        self.store = store
        self.router = Router(store)

    def call_tool(self, name, arguments):
        definitions = {tool["name"]: tool for tool in TOOLS}
        if name not in definitions:
            raise ValueError("Unknown tool")
        definition = definitions[name]["inputSchema"]
        if not isinstance(arguments, dict) or set(arguments) - set(definition["properties"]):
            raise ValueError("Unknown or invalid tool arguments")
        if set(definition.get("required", [])) - set(arguments):
            raise ValueError("Missing required tool argument")
        for key, value in arguments.items():
            field = definition["properties"][key]
            expected = field.get("type")
            valid = ((expected == "string" and isinstance(value, str)) or (expected == "object" and isinstance(value, dict)) or (expected == "integer" and isinstance(value, int) and not isinstance(value, bool)) or (expected == "number" and isinstance(value, (int, float)) and not isinstance(value, bool)))
            if not valid or ("enum" in field and value not in field["enum"]) or ("minimum" in field and value < field["minimum"]) or ("maximum" in field and value > field["maximum"]):
                raise ValueError(f"Invalid argument: {key}")
        if name == "engine_submit":
            return self.store.enqueue(arguments["spec"], arguments["request_id"])
        if name == "engine_status":
            return self.store.status()
        if name == "engine_tasks":
            return self.store.get_task(arguments["task_id"]) if "task_id" in arguments else self.store.list_tasks()
        if name == "engine_control":
            return {"control": self.store.control(arguments["mode"])}
        if name == "engine_priority":
            return self.store.set_priority(arguments["task_id"], arguments["priority"])
        if name == "engine_events":
            return self.store.events(arguments.get("after_id", 0))
        if name == "engine_artifacts":
            return self.store.artifacts(arguments.get("task_id"))
        if name == "engine_read_artifact":
            relative = Path(arguments["path"])
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValueError("Artifact path escapes private artifact storage")
            # Resolve every component relative to an already-open directory.
            # O_NOFOLLOW on the leaf alone cannot fence a replaced ancestor.
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            try:
                directory = os.open(self.store.artifact_root, directory_flags)
                try:
                    for component in relative.parts[:-1]:
                        child = os.open(component, directory_flags, dir_fd=directory)
                        os.close(directory)
                        directory = child
                    descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                finally:
                    os.close(directory)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError("Artifact paths cannot contain symlinks or non-directory ancestors") from exc
                raise
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1_000_000:
                    raise ValueError("Artifact must be a regular file up to 1 MB")
                content = stream.read(1_000_001)
                if len(content) > 1_000_000:
                    raise ValueError("Artifact grew beyond the limit")
                return {"path": arguments["path"], "text": content.decode("utf-8")}
        if name == "engine_providers":
            return self.router.list()
        if name == "engine_reserve_provider":
            return self.router.reserve(**arguments)
        if name.startswith("engine_operator_"):
            from . import scheduler
            operation = name.removeprefix("engine_operator_")
            return getattr(scheduler, f"{operation}_operator")(self.store, **arguments)
        raise ValueError("Tool has no implementation")

    def handle(self, request):
        request_id = request.get("id") if isinstance(request, dict) else None
        def error(code, message):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return error(-32600, "Invalid Request")
        if "id" not in request:
            return None
        method, params = request["method"], request.get("params", {})
        if not isinstance(params, dict):
            return error(-32602, "Invalid params")
        if method == "initialize":
            requested = params.get("protocolVersion")
            supported = {"2024-11-05", "2025-03-26", "2025-06-18"}
            result = {"protocolVersion": requested if requested in supported else "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "repvblicvs-engine", "version": __version__}, "instructions": "All clients share one persistent queue. External actions require verified adapters; no model route is ready without current evidence."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            if not isinstance(params.get("name"), str):
                return error(-32602, "Tool name required")
            try:
                value = self.call_tool(params["name"], params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(value, sort_keys=True, allow_nan=False)}], "isError": False}
                if isinstance(value, dict):
                    result["structuredContent"] = value
            except (ValueError, KeyError, RuntimeError, OSError, UnicodeError, TypeError) as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            return error(-32601, "Method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def serve(self, incoming=None, outgoing=None):
        incoming, outgoing = incoming or sys.stdin, outgoing or sys.stdout
        for line in incoming:
            if len(line.encode()) > 2_000_000:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request exceeds 2 MB"}}
            else:
                try:
                    response = self.handle(json.loads(line))
                except (ValueError, json.JSONDecodeError):
                    response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            if response is not None:
                outgoing.write(json.dumps(response, allow_nan=False) + "\n")
                outgoing.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir")
    args = parser.parse_args(argv)
    Server(Store(args.state_dir)).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
