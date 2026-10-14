"""Stage-specific access checks supplied by a trusted source reviewer.

No account access or expenditure is performed here. Receipts are references to
private evidence; listing text cannot assert its own eligibility.
"""
from __future__ import annotations

from datetime import datetime

from .opportunities import _utc, valid_source_url


COST_FIELDS = ("application_fee_cents", "participation_fee_cents", "deposit_cents", "required_purchase_cents")


def assess_route(evidence: dict, now: datetime | str | None = None) -> dict:
    """Unknown costs/process are unresolved, never evidence of free access.

    Enrollment needed only before payment does not prevent a proposal or task
    execution. A receiving route need not include immediate bank withdrawal.
    """
    route = evidence.get("route_preflight")
    if not isinstance(route, dict):
        return {"proposal_ready": False, "execution_ready": False, "payment_ready": False,
                "free_to_participate": False, "reasons": ["route_preflight_missing"]}
    reasons = []
    fresh = False
    try:
        age = (_utc(now) - _utc(route.get("checked_at"))).total_seconds() if route.get("checked_at") else None
        fresh = age is not None and 0 <= age <= 86400
    except ValueError:
        pass
    if not fresh: reasons.append("route_evidence_stale_or_missing")
    def text(value, limit=1000):
        return isinstance(value, str) and bool(value.strip()) and len(value) <= limit
    receipts = all(text(route.get(key), 512) for key in ("cost_receipt", "submission_receipt", "payout_requirements_receipt"))
    if not receipts: reasons.append("route_receipts_missing")
    costs_known = isinstance(route.get("currency"), str) and route["currency"] in {"USD", "USDC"} and all(
        isinstance(route.get(key), int) and not isinstance(route[key], bool) and 0 <= route[key] <= 100_000_000
        for key in COST_FIELDS)
    if not costs_known: reasons.append("participation_costs_unverified")
    elif any(route[key] > 0 for key in COST_FIELDS): reasons.append("upfront_spending_required")
    steps = route.get("submission_steps")
    process_known = (valid_source_url(route.get("submission_url")) and isinstance(steps, list)
                     and 1 <= len(steps) <= 12 and all(text(step) for step in steps))
    if not process_known: reasons.append("submission_process_unverified")
    prerequisites = route.get("payout_prerequisites")
    stage = route.get("payout_required_at")
    payout_known = (isinstance(stage, str) and stage in {"before_submission", "before_execution", "before_payment", "none"}
                    and isinstance(route.get("payout_ready"), bool)
                    and isinstance(prerequisites, list) and len(prerequisites) <= 12
                    and all(text(item) for item in prerequisites)
                    and (route["payout_ready"] or bool(prerequisites))
                    and (stage != "none" or route["payout_ready"]))
    if not payout_known: reasons.append("payout_prerequisites_unverified")
    base_ready = not reasons
    proposal_ready = base_ready and (route["payout_ready"] or stage != "before_submission")
    execution_ready = proposal_ready and (route["payout_ready"] or stage not in {"before_submission", "before_execution"})
    if base_ready and not execution_ready: reasons.append("payout_enrollment_required_before_" + ("submission" if stage == "before_submission" else "execution"))
    return {"proposal_ready": proposal_ready, "execution_ready": execution_ready,
            "payment_ready": base_ready and route["payout_ready"],
            "free_to_participate": fresh and receipts and costs_known and not any(route[key] for key in COST_FIELDS),
            "reasons": reasons}
