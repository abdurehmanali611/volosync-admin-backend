"""Subscription payment queue logic shared by the legacy and new endpoints.

Flow (spec §6/§7, yearly only): owner submits channel + transaction ref +
amount → PENDING row (nothing changes on the tenant) → Apex approves →
the tenant is extended 365 × years days from max(today, current expiry).
"""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .models import SubscriptionPayment

CHANNELS = {"TELEBIRR", "BANK", "CASH", "OTHER"}
STATUSES = {"PENDING", "APPROVED", "REJECTED"}


def prospective_cover_until(tenant, years):
    """Where the subscription would end if this payment were approved."""
    today = timezone.localdate()
    start = tenant.subscription_expires_on
    if not start or start < today:
        start = today
    return start + timedelta(days=365 * years)


def create_subscription_submission(user, tenant, channel, transaction_ref, amount, years, note=""):
    """Create the PENDING row. Does NOT touch the tenant's subscription."""
    today = timezone.localdate()
    payment = SubscriptionPayment.objects.create(
        tenant=tenant,
        transaction_id=transaction_ref,
        channel=(channel or "OTHER").upper(),
        status="PENDING",
        amount=amount,
        years=years,
        paid_on=today,
        covers_until=prospective_cover_until(tenant, years),
        note=note,
        submitted_by=user,
        submitted_at=timezone.now(),
    )
    return payment


def approve_subscription(payment, actor, note=""):
    """Approve a PENDING row: record the decision and extend the tenant one
    year × payment.years from max(today, current expiry)."""
    covers_until = prospective_cover_until(payment.tenant, payment.years)
    payment.status = "APPROVED"
    payment.decided_by = actor
    payment.decided_at = timezone.now()
    payment.decision_note = note or ""
    payment.covers_until = covers_until
    payment.recorded_by = actor
    payment.save(
        update_fields=[
            "status",
            "decided_by",
            "decided_at",
            "decision_note",
            "covers_until",
            "recorded_by",
        ]
    )
    tenant = payment.tenant
    tenant.subscription_transaction_id = payment.transaction_id
    tenant.subscription_paid_on = payment.paid_on
    tenant.subscription_expires_on = covers_until
    tenant.save(
        update_fields=[
            "subscription_transaction_id",
            "subscription_paid_on",
            "subscription_expires_on",
        ]
    )
    return covers_until


def reject_subscription(payment, actor, note=""):
    """Reject a PENDING row — records the decision, tenant untouched."""
    payment.status = "REJECTED"
    payment.decided_by = actor
    payment.decided_at = timezone.now()
    payment.decision_note = note or ""
    payment.save(update_fields=["status", "decided_by", "decided_at", "decision_note"])


def tenant_sales_agent_name(tenant):
    """A branch is sold with its company, so fall back to the parent's agent."""
    agent = tenant.sales_agent
    if agent is None and tenant.parent_id:
        agent = tenant.parent.sales_agent
    return agent.name if agent else ""


def payment_row(row):
    """Row shape shared by every subscription-payment listing.

    `transaction_ref` is the canonical name (owner-facing); `transaction_id`
    is kept as an alias for legacy consumers. Money is always a 2-decimal
    string (a freshly created row may still hold an unquantized Decimal).
    """
    return {
        "id": row.id,
        "tenant": row.tenant_id,
        "tenant_name": row.tenant.name,
        "tin": row.tenant.tin,
        "sales_agent_name": tenant_sales_agent_name(row.tenant),
        "transaction_id": row.transaction_id,
        "transaction_ref": row.transaction_id,
        "channel": row.channel,
        "status": row.status,
        "amount": str(row.amount.quantize(Decimal("0.01"))),
        "years": row.years,
        "paid_on": row.paid_on,
        "covers_until": row.covers_until,
        "note": row.note,
        "submitted_by": row.submitted_by.username if row.submitted_by else "",
        "submitted_at": row.submitted_at,
        "decided_by": row.decided_by.username if row.decided_by else "",
        "decided_at": row.decided_at,
        "decision_note": row.decision_note,
        "recorded_by": row.recorded_by.username if row.recorded_by else "",
        "created_at": row.created_at,
    }
