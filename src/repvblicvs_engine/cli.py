"""Repvblicvs command interface; every frontend uses the same private state."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from .store import Store
from .providers import Router


def _json_input(value):
    if value == "-":
        content = sys.stdin.read()
    elif value.lstrip().startswith("{"):
        content = value
    else:
        content = Path(value).read_text()
    return json.loads(content)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="Private runtime directory; defaults to REPVBLICVS_STATE_DIR")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    submit = commands.add_parser("submit")
    submit.add_argument("spec", help="JSON spec file, inline JSON, or - for stdin")
    submit.add_argument("--request-id")
    run = commands.add_parser("run")
    run.add_argument("--once", action="store_true", help="Process at most one runnable task")
    run.add_argument("--max-tasks", type=int)
    run.add_argument("--poll-interval", type=float, default=2)
    status = commands.add_parser("status")
    status.add_argument("--task-id")
    for command in ("pause", "resume", "stop", "providers", "tasks", "actions"):
        commands.add_parser(command)
    artifacts = commands.add_parser("artifacts")
    artifacts.add_argument("--task-id")
    events = commands.add_parser("events")
    events.add_argument("--after-id", type=int, default=0)
    priority = commands.add_parser("priority")
    priority.add_argument("task_id")
    priority.add_argument("priority", type=int)
    retry = commands.add_parser("retry")
    retry.add_argument("task_id")
    service = commands.add_parser("service")
    service.add_argument("action", choices=["install", "uninstall", "status", "render"])
    service.add_argument("--python")
    commands.add_parser("mcp")
    operator = commands.add_parser("operator", help="Trusted connector frontend obligation bridge")
    operator.add_argument("action", choices=["claim", "complete", "defer", "renew"])
    operator.add_argument("--operator-id", required=True)
    operator.add_argument("--receipt-id", required=True)
    operator.add_argument("--task-id")
    operator.add_argument("--lease-token")
    operator.add_argument("--lease-seconds", type=float, default=300)
    operator.add_argument("--evidence", help="Bounded JSON object file or inline JSON")
    operator.add_argument("--result", help="Bounded JSON object file or inline JSON")
    operator.add_argument("--reason")
    args = parser.parse_args(argv)
    try:
        if args.command == "service":
            from .service import main as service_main
            service_args = [args.action]
            if args.state_dir:
                service_args.extend(["--state-dir", args.state_dir])
            if args.python:
                service_args.extend(["--python", args.python])
            return service_main(service_args)
        store = Store(args.state_dir)
        if args.command == "mcp":
            from .mcp import Server
            Server(store).serve()
            return 0
        if args.command == "init":
            result = store.status()
        elif args.command == "submit":
            if args.spec == "-":
                content = sys.stdin.read()
            elif args.spec.lstrip().startswith("{"):
                content = args.spec
            else:
                content = Path(args.spec).read_text()
            result = store.enqueue(json.loads(content), args.request_id)
        elif args.command == "run":
            from .daemon import Worker
            import signal
            if args.poll_interval <= 0 or (args.max_tasks is not None and args.max_tasks < 1):
                raise ValueError("Positive poll interval and max-tasks required")
            worker = Worker(store)
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: worker.shutdown.set())
            result = worker.step() if args.once else {"processed": worker.run(poll_interval=args.poll_interval, max_tasks=args.max_tasks)}
        elif args.command == "status":
            result = store.get_task(args.task_id) if args.task_id else store.status()
        elif args.command == "tasks":
            result = store.list_tasks()
        elif args.command in {"pause", "resume", "stop"}:
            result = {"control": getattr(store, args.command)()}
        elif args.command == "providers":
            result = Router(store).list()
        elif args.command == "artifacts":
            result = store.artifacts(args.task_id)
        elif args.command == "events":
            result = store.events(args.after_id)
        elif args.command == "actions":
            result = store.list_actions()
        elif args.command == "priority":
            result = store.set_priority(args.task_id, args.priority)
        elif args.command == "retry":
            result = store.retry_deferred(args.task_id)
        elif args.command == "operator":
            from . import scheduler
            common = {"operator_id": args.operator_id, "receipt_id": args.receipt_id}
            if args.action == "claim":
                result = scheduler.claim_operator(store, **common, task_id=args.task_id, lease_seconds=args.lease_seconds)
            elif args.action == "complete":
                if not args.evidence or not args.result:
                    raise ValueError("Completion requires --evidence and --result")
                result = scheduler.complete_operator(store, **common, lease_token=args.lease_token, evidence=_json_input(args.evidence), result=_json_input(args.result))
            elif args.action == "defer":
                result = scheduler.defer_operator(store, **common, lease_token=args.lease_token, reason=args.reason)
            else:
                result = scheduler.renew_operator(store, **common, lease_token=args.lease_token, lease_seconds=args.lease_seconds)
        else:
            raise ValueError("Unsupported command")
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, KeyError, RuntimeError, OSError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
