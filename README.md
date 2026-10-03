# Repvblicvs Engine

Repvblicvs turns data and document work into reproducible delivery packages. The engine keeps tasks, artifacts, validation results and delivery receipts together, so work can be resumed and checked rather than reconstructed from a chat.

## What it does

- **Data repair:** normalize CSV structure, preserve missing values, analyze totals with decimal arithmetic, and produce a cleaned dataset with a replay script and validation report.
- **Document production:** package source text as Markdown and standalone HTML with a manifest; optional geometric SVG illustrations support a bounded set of subjects.
- **Exact analysis:** explore bounded polynomial and recurrence models with rational arithmetic, held-out checks and explicit uncertainty.
- **Operations:** share one durable queue across compatible CLI, MCP and desktop clients, with request deduplication, write leases and recorded external-action outcomes.

The examples use synthetic data. They demonstrate supported behavior, not prior customer engagements or general scientific discoveries. See [workflow contracts](docs/commercial-workflows.md) for input formats and limits.

## Install and run

Python 3.12 or later is required. The runtime has no third-party dependencies.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/repvblicvs init
.venv/bin/repvblicvs submit examples/data-repair.json --request-id example-data-repair
.venv/bin/repvblicvs run --once
.venv/bin/repvblicvs status
.venv/bin/repvblicvs artifacts
```

Runtime state belongs outside the source checkout. By default it uses the platform's Application Support directory. Set `REPVBLICVS_STATE_DIR` to select a private location; every connected client must use that same location.

## Shared controls

`repvblicvs-mcp` exposes the shared queue, results, priorities and lifecycle controls. Register the bridge in each client. A client connection provides access to the same local state; it does not authenticate a model provider or establish marketplace permissions.

```sh
repvblicvs pause
repvblicvs resume
repvblicvs stop
repvblicvs service install
repvblicvs service status
```

The macOS service uses a per-user LaunchAgent and managed wake assertions. It checkpoints work and can restart after a worker failure. Display locking, lid closure, system power events and provider limits can still interrupt desktop actions; keep recoverable state and verify the deployment's actual behavior.

## Privacy and delivery

Customer material, credentials, account settings and operating records stay private. Public exports are checked before commit, in CI and after package construction. These checks supplement a review of the exact content and its intended audience.

Model routes require current entitlement and allowance evidence. External communications require a reviewed payload and an actual connector receipt. A prepared offer is not an accepted contract, and a delivery receipt is not payment settlement.

## Development

```sh
.venv/bin/python -m pip install '.[test]'
.venv/bin/python -m pytest -q
```

See [operator controls](docs/operator.md), [validation](docs/release-evidence.md), [selected research capabilities](docs/portfolio-components.md).
