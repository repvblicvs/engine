# Commercial operation and delivery contracts

Revenue work takes priority over independent exploratory research. `Commerce`
uses the same private `Store` database and outbox as CLI, MCP, and the worker. It
does not start a second queue or call external services itself.

## Delivery workflows

`run_workflow(kind, payload, output_dir)` accepts an empty private output directory
and returns relative `artifacts`, a structured `summary`, and check `evidence`.
Failures raise `WorkflowError` with a stable `code` and `message`.

- `csv_cleanup`: provide exactly one of `csv_text` or UTF-8 `input_path`. Optional
  `delimiter` is comma, semicolon, tab, or pipe. `remove_duplicates` defaults to
  false. Headers are normalized uniquely, whitespace is trimmed, blank rows are
  counted, textual spreadsheet formulas are escaped, and missing data is never
  imputed. Optional `acceptance` supports `expected_output_rows`,
  `required_columns`, and `numeric_columns` using normalized names. The delivery
  includes cleaned CSV, source-header mapping, decimal analysis, replay script,
  report, and SHA-256 manifest. Numeric classification is bounded; decimal totals
  use adequate precision and rounded means report their precision.
- `document_package`: provide `title` and `content` (or `brief`). Optional
  `required_terms` checks acceptance. Optional `sources` contain title/URL pairs;
  URL structure is verified and substantive source review remains explicit. The
  delivery includes Markdown, escaped standalone HTML, and a manifest. An explicit
  `illustration` may request a non-explicit original geometric fox, cat, wolf, or
  rabbit `portrait`. This deterministic SVG capability is not a generative image
  model and is not an unrestricted commercial illustration offer.
- `research_sequence`: provide 2–32 exact integer/rational `values`; float inputs
  are rejected. `holdout` defaults to two, `max_degree` to four, and `max_order`
  to three. Results classify supported-within-bounds, ambiguous, and unsupported
  cases. The package includes an exact-rational experiment and replay script;
  finite agreement is not a generating-rule proof or novelty claim.
- `json_preflight`: provide `text` or its compatibility alias `csv_text`, exactly
  one at a time. The package preserves the original UTF-8 bytes and reports
  bounded structural JSON findings, including duplicate keys.
- `catalog_inspect`: provide exactly one of `text` or `csv_text`. Optional `mode`
  is `create` or `update`; `header_style` is `current` or `legacy`. The original
  bytes and bounded catalog findings are packaged without altering the input.

Inspection completion records package production; the report separately states
whether the source passed structural checks. Unsupported options and acceptance
criteria are refused before creating an output directory through either
`run_workflow` or the direct `run_inspection` entry point.

Synthetic fixtures in `examples/` demonstrate data and document deliveries.
Customer artifacts remain private unless the customer explicitly permits release.

## Qualification and opportunity experiments

`Commerce.register_opportunity(opportunity, scope, evidence, capabilities)` accepts
a public HTTP(S) solicitation, a known category, explicit permitted AI use, low
risk, and an available delivery capability. Unknown AI eligibility is deferred.
Personal attestations, unavailable credentials, and new spending are disqualified.

The operator supplies verified `source_read_receipt`, `scope_receipt`,
`ai_permission_receipt`, `currently_open: true`, and timezone-aware `checked_at`.
Evidence expires after 24 hours for new customer acquisition. Intake text is inert
data and cannot override business policy, choose a shell command, or change tools.
Future-dated source or merchant evidence is unavailable until its timestamp is
current; a timestamp within the next 24 hours is not fresh evidence.
Scope lists deliverables and measurable acceptance, estimated minutes, and optional
exclusions. Existing scope cannot silently overwrite a customer's agreement.

Route readiness additionally requires a private `evidence.route_preflight` record.
Review the actual submission route before investing in onboarding or a proposal.
Supply a timezone-aware `checked_at`, `currency` (`USD` or `USDC`), and explicit
nonnegative integer `application_fee_cents`, `participation_fee_cents`,
`deposit_cents`, and `required_purchase_cents`. Zero must be evidenced; an unknown
cost is not free access. A refundable deposit is still an upfront expenditure.
`cost_receipt` references that verification. New upfront spending defers that
route without blocking other opportunities.

Record the actual `submission_url`, bounded `submission_steps`, and
`submission_receipt`. Record known `payout_prerequisites`, a boolean `payout_ready`,
and `payout_requirements_receipt`. `payout_required_at` is `before_submission`,
`before_execution`, `before_payment`, or `none`. Pending enrollment blocks only
the stage that requires it: enrollment required before payment can coexist with a
ready proposal and executable task. A verified receiving method does not require
an immediate bank withdrawal path. These fields report requirements and verified
access; they do not accept terms, create an account, or authorize spending.

Assessments expose `proposal_ready`, `execution_ready`, `payment_ready`, and
`free_to_participate` separately. Missing evidence leaves the route unresolved.
Both registration methods replace the opportunity's current `route_preflight`
record. Preparation and dispatch use that authoritative record, so a later fee or
missing-evidence review supersedes an older inquiry review.

Applications and their dispatch recheck proposal readiness; qualification and new
commercial actions require execution readiness. Prepared applications and
qualified contacts include a `submission_route` snapshot and
`submission_route_hash` binding the reviewed costs, submission URL, steps,
receipts, and payout prerequisites. Dispatch compares the current route with that
snapshot before releasing the connector payload. A timestamp-only freshness
refresh retains the review; changed route content or receipts require a newly
reviewed intent. Legacy prepared intents without a bound route fail this check.

A permitted free inquiry can clarify unknown costs without presenting the route
as ready. Its reviewed contact target and contact-permission receipt are checked
again before dispatch. Existing SQLite records require fresh route evidence for
new applications and qualified contacts; accepted customer obligations remain
preserved. No database schema migration is required.

`tests/test_route_preflight.py` exercises fee updates through both registration
paths, changed free submission routes, legacy intents, freshness-only refreshes,
and connector payload refusal before an external action. These regressions use
synthetic records and transport callbacks.

When a real solicitation leaves AI permission or scope unknown, use
`register_solicitation(opportunity, evidence, now=...)` and
`prepare_inquiry(opportunity_id, action_key, questions=None, kind="initial", now=...)`.
This is a nonbinding question, never an accepted job or qualified-contact count.
The source must invite work or contributions, the contact channel must be published
for an appropriate public inquiry, and the request must be currently open and low
risk. Explicitly forbidden AI, archived sources, unsolicited pitches, personal
attestations, unavailable credentials, and new spending remain blocked. Unknown
capability may be clarified without claiming that capability exists.

The trusted source record requires `source_read_receipt`, `solicitation_receipt`,
`contact_permission_receipt`, `currently_open: true`, `archived: false`,
`public_contact_permitted: true`, and a timezone-aware `checked_at` within 24 hours.
`customer_reference` identifies the verified target. A conflicting
`repository_archived: true` blocks inquiry even if another field says otherwise.
Recheck the source before dispatch; preparation does not freeze old permission.

Inquiry questions are fixed purpose codes: `funding`, `availability`,
`ai_eligibility`, `scope`, `acceptance`, `assignment`, and `payment`. Selecting codes
produces the corresponding bounded questions; arbitrary source instructions or
outbound assertions cannot replace the template. The message discloses AI operation,
asks about eligibility, and assumes no assignment, capability, price or payment.
It never claims the source already permits AI. Once the customer supplies terms,
register a fully verified opportunity before quoting or accepting a scope.

For an already priced request with a published application channel, the trusted
operator can instead prepare a tailored response with
`prepare_application(opportunity_id, action_key, subject=..., proposal=...,
review_receipt=..., kind="initial", now=...)`. This uses the same solicitation
and contact-permission gates; it allows unknown supplier eligibility to be
clarified without claiming it is established. A proposal must be actually reviewed
by the trusted operator for truthful capability claims and appropriate fit.
`review_receipt` records that review; it is private metadata, not an assertion of
independent human technical validation. Subject, proposal and review receipt are
bounded at 200, 6,000 and 1,000 characters. Control characters and multiline
subjects are rejected. Source text cannot supply a proposal or change policy.

The message always includes the AI-operated-business disclosure and a nonbinding
qualification acknowledgement: it assumes no assignment, delivery commitment,
eligibility, price agreement or payment authorization. `commerce_application`
has `qualification_only: true`; preparation and confirmed delivery neither
qualify a job nor create a quote or agreement. Evidence from the future is refused,
and dispatch rechecks the current source and original verified contact target.
Register a fully verified opportunity and customer agreement separately before
accepting paid work. The operator JSON action prepares only an unsent outbox item:

```json
{
  "operation": "application",
  "args": {
    "opportunity_id": "previously-registered-solicitation-id",
    "action_key": "solicited-application-unique-key",
    "subject": "Response to your data cleanup request — Repvblicvs",
    "proposal": "Your posted scope and budget appear suitable for a discussion. We propose a reproducible cleanup package and audit report. Please confirm AI-operated supplier eligibility and the exact acceptance criteria.",
    "review_receipt": "actual-trusted-operator-proposal-review-receipt"
  }
}
```

Run through `python -m repvblicvs_engine.operator request.json` using the canonical
private state. Inspect the prepared text before the existing `begin`/`receipt`
connector procedure. Keep the review receipt private; only the reviewed subject
and message belong in the customer communication.

`seed_experiments` starts three offers with advisory price bands: dataset repair
and automation ($150–600); one reproducible software fix with a regression test
($200–900); and a sourced document/report/presentation ($100–500). These bands are
defaults, not promises; quotes require bounded verified scope and available tools.
At most three offers are active. Ten qualified contacts or seven days produces a
persisted continue/adjust/replace decision using interest and cleared contribution.
Replacing retires the old offer and leaves its receipts intact; the trusted
operator selects and activates the next bounded demand experiment.

`revise_experiment(experiment_id, offer, capability, price_band_cents, evidence,
replacement_id=None, now=...)` applies that decision without an owner prompt.
It is a trusted-operator action, not an instruction accepted from customer text.
A due `adjust` review can revise its active offer once. A due `replace` review
requires the original to be retired and `replacement_id` to be completely fresh;
activation uses the same maximum of three active experiments. A `continue` decision
does not permit revision. Re-running seed does not revive a retired offer.

Supported capabilities are `csv_cleanup`, `document_package`, and `software_fix`.
Offers are bounded text; an advisory price band is two ordered positive integer
USD-cent amounts, each at most 1,000,000 cents. These prospective offer ranges do
not modify any existing quote, scope, customer agreement, or invoice.

Evidence requires `receipt`, `kind`, `finding`, and timezone-aware `checked_at`.
`kind` is `primary_source`, `market_feedback`, or `delivery_results`. Primary
source evidence additionally needs a valid public `source_url`; actual private
feedback or delivery evidence can use its verified customer/ledger receipt.
The finding must state the observed demand or result that justifies the change.
Future timestamps, empty receipts/findings, unsupported kinds and arbitrary command
fields are rejected. The operator must supply actual evidence, not generated claims
that a request, customer reply or payment occurred.

The receipt is the immutable revision idempotency key. Replaying the exact request
returns its existing result; reusing that receipt with different content conflicts.
A second new receipt cannot apply the same review again. The database retains the
evaluated offer, every due review, the old offer/price snapshot, revision evidence,
and replacement links, and emits a `commercial_experiment_revised` event.

## Connector transport, outreach, and payment gates

A prepared quote is bound to its reviewed customer recipient. Dispatch refuses
an opportunity refresh that changes that recipient. Scope agreement records the
recipient from the delivered quote, and invoices use that accepted recipient even
if the listing later changes or closes. Existing agreements without a stored
recipient require matching delivered-quote evidence before billing can proceed;
an unresolved recipient does not delete the customer obligation.

The trusted operator supplies a `Transport` with `send`, `invoice`, `receipt`,
`search`, and `read` callbacks using existing authorized connectors. The package
never guesses credentials. Actual transport readiness requires separately recorded
connector evidence; protocol support alone does not establish a live connection.

Prepared contacts contain tailored deliverables, acceptance, and truthful AI use.
The owner can remove the global daily contact restriction through private operator
policy. `Commerce.set_contact_policy(daily_contact_limit=None,
max_dispatch_batch=20, authorization_receipt="<owner instruction receipt>")`
records that explicit authorization and its history in the same SQLite business
state used by reservation and dispatch. It performs no external action. A null
limit applies to both initial contacts and eligible follow-ups; it does not impose
a replacement daily ceiling. Existing stores without this setting retain the
legacy three-per-day behavior until an authorized operator changes it.
`commerce_inquiry`, `commerce_application` and `commerce_contact` share customer
deduplication, source qualification, follow-up policy, outbox and idempotency state.
Uncertain inquiries and applications cannot be retried without a verified not-sent
receipt. Neither counts as a qualified experiment contact.
Only one automatic follow-up per opportunity is eligible after three business days
(weekends excluded). Replies,
declines, and unsubscribes suppress inappropriate automatic follow-ups.

`dispatch_batch(action_ids, transport)` accepts an explicit reviewed list of
distinct contact actions and performs at most the configured finite number per
invocation (20 by default, configurable from 1–100). An oversized or malformed
batch is rejected before any connector callback. Further reviewed batches may run
the same day as capacity and suitable opportunities allow. This invocation bound
prevents a single runaway loop; it is not a daily contact quota. Every action still
passes its own current-source and customer gates. Batch dispatch excludes quotes
and invoices. Three simultaneous offer experiments remain the independent demand
testing limit, regardless of the number of appropriate customer contacts.

Dispatch commits a `dispatching` state before invoking a connector. Confirmed
actions require an `external_ref`; replay returns the existing receipt without
calling again. Timeouts and crash outcomes become `unknown`. `reconcile` only reads
receipts; it never resends. A verified `not_sent` receipt permits a controlled retry.
An in-flight `dispatching` call receives a two-minute grace period before receipt
reconciliation, preventing a live send from racing a premature "not found" query.
Uncertain attempts retain their reservations until reconciled. If an operator has
configured a finite daily limit, uncertain attempts count against it and a queued
contact crossing midnight must claim a slot on its actual send day. With the daily
limit removed, neither preparation nor dispatch blocks a fourth suitable contact.

Merchant readiness must verify a live account, charges/payouts/details enabled,
no currently due requirements, and customer-facing business name, support contact,
and statement descriptor. Verification expires after 24 hours. Quotes require an
established customer conversation; invoices additionally require a receipt of the
customer's exact price and scope agreement. Invoices do not authorize automatically
charging stored payment methods. A subsequently closed solicitation does not erase
an accepted customer obligation.

## Accounting and research capacity

Record business events with an immutable receipt and idempotency key. Quoted,
agreed, delivered, paid, refunded, fees, compute costs, gross settled amounts, and
project funds becoming available remain distinct. Cleared contribution is gross
settled less refunds, fees, and compute costs. Usable project funds cannot exceed
that contribution, even if an account contains unrelated funds. This ledger never
grants spending authority.

`reserve_execution` and `complete_execution` account for measured work units.
Customer delivery has priority 100, acquisition 80, revenue products 60,
maintenance 40, and independent exploratory research 0. Exploratory reservations
cannot exceed 10% of executed capacity and are refused while higher-priority work
is queued or running. Overruns are recorded honestly and block further research
capacity rather than disappearing from accounting. The scheduler/operator must
use these reservations for exploratory work; related customer research is delivery.

`scan_public_export` blocks credential patterns, non-allowlisted emails, private
paths/customer markers, unsafe archives, unreviewed binary data, and audit limits.
It reports locations/classifications without matched values. Pattern checks
supplement substantive release review; a clean scan is not proof of complete privacy.

Private operating summaries are excluded by filename and recognizable headings,
including when renamed or placed inside a package. Public documentation describes
the product, its verified behavior and limitations. Session reports and internal
coordination belong outside public repositories and release artifacts.
