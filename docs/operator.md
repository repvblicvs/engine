# Operating the shared engine

The CLI, MCP bridge and compatible desktop clients use one private SQLite store. Pause and stop prevent new claims while active work drains. A model, webpage or customer document cannot grant account permissions or change operating policy.

## Task ownership

Use request IDs to deduplicate submissions and write leases for shared changes. A trusted frontend claims an operator obligation with an explicit identity and receipt ID, then supplies evidence when completing or deferring it. Identity labels provide provenance; the host supplies account authorization.

```sh
repvblicvs operator claim --operator-id ID --receipt-id UNIQUE_ID
repvblicvs status
repvblicvs pause
```

No frontend is a compulsory intermediary. A task's responsible operator may use its existing authorized connector and record the result in the shared store. Review of delegated output and responsibility for final validation remain explicit.

The deterministic worker assigns a fresh lease owner to each claim. Direct
`Store` clients must also use a unique owner for each claim and keep that owner
with its heartbeat, checkpoints and final transitions. Reusing an owner across
attempts prevents the owner-string API from distinguishing stale callbacks.
A durable `workflow_finished` checkpoint can be finalized after the last allowed
execution attempt without running the workflow again or consuming another retry.
An expired worker's failure or deferral cannot overwrite a replacement claim.

MCP artifact reads walk each path component through open directory descriptors.
Symlinks, traversal paths and nonregular files are refused; replacing an ancestor
with a symlink does not redirect the read outside artifact storage. The 1 MB
content bound applies before and during reading.

## External actions

`repvblicvs-operator REQUEST.json` prepares operations in private state. Keep request files outside the repository.

1. Verify the current source, destination, capability and applicable customer requirements.
2. Review the precise outgoing payload.
3. Reserve the attempt with `begin`.
4. Use the authorized connector once.
5. Record its observed external identifier with `receipt`.

An unknown outcome requires readback and reconciliation, not another send. The bridge does not bypass a platform's identity, terms, account or transaction controls. Use the matching platform route rather than substituting an unrelated email channel.

## Provider routing

Configure verified model identity, account grouping, tool compatibility and current allowance. Routes on one account share limits. A reservation controls the local authorization envelope; it is not independent billing evidence.

Select a suitable route for the work's complexity and tools. Preserve requirements when switching routes. Keep account-specific budgets, balances, authorization receipts and routing decisions in private configuration. Do not put them in product documentation.

## Lifecycle

Use an installed virtual environment for service operation. The macOS LaunchAgent supervises the deterministic worker. Managed wake assertions do not prevent every sleep or power event, and an unlocked desktop is necessary for UI actions.

Service replacement verifies that the previous job unloaded before replacing its
installed configuration. A failed unload or state inspection leaves that
configuration available for inspection and retry. Installation reports success
only for a loaded job, and uninstall preserves the configuration if the job
cannot be unloaded. These checks do not establish physical reboot or sleep/wake
recovery.

Before closing a client, save its work, record outstanding obligations and release leases. Completed research or source-collection work does not establish customer delivery. Use compact status checks and close completed task tabs to limit resource use.
