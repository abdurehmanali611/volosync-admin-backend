"""Subscription payment queue logic shared by the legacy and new endpoints.

Flow (spec §6/§7, yearly only): owner submits channel + transaction ref +
amount → PENDING row (nothing changes on the tenant) → Apex approves →
the tenant is extended 365 × years days from max(today, current expiry).
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import SetupPayment, SubscriptionPayment, Tenant
from .pricing import suggest_fees

CHANNELS = {"TELEBIRR", "CBE", "BANK", "CASH", "OTHER"}
STATUSES = {"PENDING", "APPROVED", "REJECTED"}

User = get_user_model()


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
    # The approval starts the paid year: retire the free trial so its 10-day
    # warning and any trial-based login block do not outlive the payment.
    tenant.free_trial_ends_on = None
    tenant.save(
        update_fields=[
            "subscription_transaction_id",
            "subscription_paid_on",
            "subscription_expires_on",
            "free_trial_ends_on",
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


# ---------------------------------------------------------------------------
# Branch requests
# ---------------------------------------------------------------------------


def family_of(parent):
    """The company plus every satellite, main branch first."""
    members = [parent, *parent.branches.all()]
    members.sort(key=lambda tenant: (not tenant.is_main, tenant.name.lower()))
    return members


def fees_for_modules(module_inventory, module_hr, sales_role_enabled):
    """Catalog ``(plan, yearly, setup)`` for a module mix that has no tenant yet."""
    probe = Tenant(
        module_inventory=bool(module_inventory),
        module_hr=bool(module_hr),
        sales_role_enabled=bool(sales_role_enabled),
    )
    return suggest_fees(probe)


def branch_request_row(row):
    """The shape both the global queue and the company Branches tab render."""
    return {
        "id": row.id,
        "parent": row.parent_id,
        "parent_name": row.parent.name,
        "parent_tin": row.parent.tin,
        "name": row.name,
        "tin": row.tin,
        "phone": row.phone,
        "address": row.address,
        "vat_rate": str(row.vat_rate),
        "module_inventory": row.module_inventory,
        "module_hr": row.module_hr,
        "sales_role_enabled": row.sales_role_enabled,
        "make_main": row.make_main,
        "manager_name": row.manager_name,
        "manager_username": row.manager_username,
        "setup_channel": row.setup_channel,
        "setup_transaction_ref": row.setup_transaction_ref,
        "setup_amount": str(row.setup_amount.quantize(Decimal("0.01"))),
        "status": row.status,
        "note": row.note,
        "decision_note": row.decision_note,
        "created_by": row.created_by.username if row.created_by else "",
        "created_at": row.created_at,
        "decided_by": row.decided_by.username if row.decided_by else "",
        "decided_at": row.decided_at,
        "created_branch": row.created_branch_id,
        "created_branch_tin": row.created_branch.tin if row.created_branch else "",
    }


def approve_branch_request(row, actor, note=""):
    """Turn a PENDING branch request into a live branch.

    Creates the Tenant, activates the manager login with the stored password
    hash, records the attached setup receipt as APPROVED and (optionally)
    promotes the branch to main. Returns the new Tenant. Raises ValueError
    with a human message when the request can no longer be honoured.
    """
    if row.status != "PENDING":
        raise ValueError("This branch request was already decided.")

    if Tenant.objects.filter(tin=row.tin).exists():
        raise ValueError(
            f"TIN {row.tin} is already registered — decline this request and ask the owner to resubmit."
        )
    username = (row.manager_username or "").strip()
    if username and User.objects.filter(username__iexact=username).exists():
        raise ValueError(
            f"The manager username “{username}” is already taken — decline this request and ask the owner to pick another."
        )

    plan, yearly, setup = fees_for_modules(
        row.module_inventory, row.module_hr, row.sales_role_enabled
    )
    del plan
    with transaction.atomic():
        branch = Tenant.objects.create(
            name=row.name,
            tin=row.tin,
            phone=row.phone,
            address=row.address,
            vat_rate=row.vat_rate,
            module_inventory=row.module_inventory,
            module_hr=row.module_hr,
            sales_role_enabled=row.sales_role_enabled,
            parent=row.parent,
            is_main=False,
            annual_fee=yearly if yearly is not None else Decimal("0.00"),
            setup_fee=setup if setup is not None else Decimal("0.00"),
        )
        if username and row.manager_password_hash:
            user = User(
                username=username,
                password=row.manager_password_hash,
                first_name=row.manager_name or row.name,
                last_name="",
            )
            user.save()
            from user.models import Profile

            Profile.objects.create(user=user, tenant=branch, role="MANAGER")
        if row.setup_transaction_ref:
            SetupPayment.objects.create(
                tenant=branch,
                amount=row.setup_amount,
                transaction_ref=row.setup_transaction_ref,
                channel=row.setup_channel or "OTHER",
                status="APPROVED",
                note="Approved with branch request",
                submitted_by=row.created_by,
                submitted_at=row.created_at,
                decided_by=actor,
                decided_at=timezone.now(),
                decision_note=note,
            )
        if row.make_main:
            Tenant.objects.filter(Q(pk=row.parent_id) | Q(parent=row.parent)).update(
                is_main=False
            )
            branch.is_main = True
            branch.save(update_fields=["is_main"])

        row.status = "APPROVED"
        row.decided_by = actor
        row.decided_at = timezone.now()
        row.decision_note = note or ""
        row.created_branch = branch
        row.save(
            update_fields=[
                "status",
                "decided_by",
                "decided_at",
                "decision_note",
                "created_branch",
            ]
        )
    return branch


def reject_branch_request(row, actor, note):
    row.status = "REJECTED"
    row.decided_by = actor
    row.decided_at = timezone.now()
    row.decision_note = note
    row.save(update_fields=["status", "decided_by", "decided_at", "decision_note"])


# ---------------------------------------------------------------------------
# Multi-branch yearly payments
# ---------------------------------------------------------------------------


def branch_payment_row(row):
    lines = list(row.lines.select_related("tenant").all())
    included = [line for line in lines if line.status != "EXCLUDED"]
    approved = [line for line in lines if line.status == "APPROVED"]
    return {
        "id": row.id,
        "parent": row.parent_id,
        "parent_name": row.parent.name,
        "parent_tin": row.parent.tin,
        "transaction_ref": row.transaction_ref,
        "channel": row.channel,
        "years": row.years,
        "amount": str(row.amount.quantize(Decimal("0.01"))),
        "approved_amount": str(
            sum((line.amount for line in approved), Decimal("0.00")).quantize(
                Decimal("0.01")
            )
        ),
        "status": row.status,
        "note": row.note,
        "decision_note": row.decision_note,
        "submitted_by": row.submitted_by.username if row.submitted_by else "",
        "submitted_at": row.submitted_at,
        "decided_by": row.decided_by.username if row.decided_by else "",
        "decided_at": row.decided_at,
        "created_at": row.created_at,
        "branches": [
            {
                "line_id": line.id,
                "tenant": line.tenant_id,
                "name": line.tenant.name,
                "tin": line.tenant.tin,
                "is_main": line.tenant.is_main,
                "amount": str(line.amount.quantize(Decimal("0.01"))),
                "years": line.years,
                "status": line.status,
                "covers_until": line.covers_until,
                "approved_until": line.approved_until,
                "decision_note": line.decision_note,
            }
            for line in lines
        ],
        "included_count": len(included),
        "approved_count": len(approved),
    }


def approve_branch_payment(row, actor, included_line_ids=None, note=""):
    """Approve some (or all) lines of a multi-branch payment.

    Included lines extend from each branch's own expiry and write a normal
    APPROVED SubscriptionPayment row. Excluded lines stay unpaid and can be
    resubmitted by the owner.
    """
    if row.status != "PENDING":
        raise ValueError("This payment request was already decided.")
    lines = list(row.lines.select_related("tenant").all())
    if not lines:
        raise ValueError("This payment request has no branches.")
    wanted = (
        {int(value) for value in included_line_ids}
        if included_line_ids is not None
        else {line.id for line in lines}
    )
    keep = [line for line in lines if line.id in wanted]
    drop = [line for line in lines if line.id not in wanted]
    if not keep:
        raise ValueError("Keep at least one branch in the approval.")

    now = timezone.now()
    with transaction.atomic():
        for line in keep:
            payment = create_subscription_submission(
                row.submitted_by,
                line.tenant,
                row.channel,
                row.transaction_ref,
                line.amount,
                line.years,
                row.note,
            )
            covers = approve_subscription(payment, actor, note)
            line.status = "APPROVED"
            line.approved_until = covers
            line.decision_note = note
            line.save(update_fields=["status", "approved_until", "decision_note"])
        for line in drop:
            line.status = "EXCLUDED"
            line.decision_note = note
            line.save(update_fields=["status", "decision_note"])

        row.status = "APPROVED" if not drop else "PARTIALLY_APPROVED"
        row.decided_by = actor
        row.decided_at = now
        row.decision_note = note or ""
        row.save(
            update_fields=["status", "decided_by", "decided_at", "decision_note"]
        )
    return row


def reject_branch_payment(row, actor, note):
    if row.status != "PENDING":
        raise ValueError("This payment request was already decided.")
    now = timezone.now()
    with transaction.atomic():
        row.lines.update(status="REJECTED", decision_note=note)
        row.status = "REJECTED"
        row.decided_by = actor
        row.decided_at = now
        row.decision_note = note
        row.save(
            update_fields=["status", "decided_by", "decided_at", "decision_note"]
        )
    return row


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
