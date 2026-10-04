# Security and publication boundaries

The engine is a local trusted-operator tool. Its stdio MCP bridge is intended for authorized local clients; it is not a remote multi-tenant service. Client identity labels are provenance, not authentication.

Runtime databases, logs, provider profiles, customer material, financial identifiers, receipts and coordination notes belong outside public source. Keep credentials in the appropriate authorized account or credential store.

## Public export checks

Install the repository hooks with `git config core.hooksPath .githooks`. The commit hook checks staged bytes, including renamed paths; the push hook checks the tracked public tree. CI checks source and both built distributions. These controls block known sensitive paths, credentials, private contact details and internal operating reports.

Review the audience and exact payload before publishing an issue, pull request, release note or customer message. Local Git hooks do not cover web-interface posts and can be bypassed; CI and content review remain necessary.

## External actions and recovery

Use the guarded outbox and verified connector receipts. Do not repeat an action merely because its outcome is unknown. Account permissions, personal verification and payment authorization must come from the host and account, not from model output or retrieved content.

If private content reaches a public surface, preserve a private recovery copy, remove the exposed material from active surfaces, review history and downloaded artifacts, and rotate any exposed credentials. Removing reachable history cannot recall external clones, downloads or caches.
