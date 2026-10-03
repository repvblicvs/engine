# Validation and release checks

Each release is bound to its exact source commit, tests and package hashes. Evidence describes the tested behavior and its limits. Account records, customer communications and internal operational reports are retained privately.

## Required checks

- Run the current source tests and current-commit CI.
- Build the wheel and source distribution.
- Install the wheel into a clean environment.
- Run the synthetic data and document examples using that installation.
- Check source, staged content and built archives for private material.
- Preserve license notices and trace reused components to their source.
- Bind release assets to checksums and the source commit.

## Recovery and interoperability

Test durable request deduplication, write leases, incomplete attempts and confirmed external-receipt recovery. A process restart test is different from a physical reboot, sleep/wake event or live network interruption; identify the actual condition demonstrated.

Clients must submit and inspect work in the same queue. Registration alone is not an interoperability test. Provider configuration and marketplace authorization are separate from shared-state access.

## Commercial evidence

Synthetic examples are capability demonstrations. Distinguish prepared offers, submitted offers, accepted scope, delivered work, payments and settlement. Record private identifiers in private evidence rather than public release notes.

## Evidence boundaries

Pattern checks cannot establish that every form of private information was detected. Review the intended public audience and exact exported content. Do not publish internal coordination, account allowance, contact lists, customer data or session reports as release evidence.
