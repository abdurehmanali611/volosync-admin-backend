"""Owner-facing payment submissions + the Apex approval decision.

    POST  /api/payments/subscription/              submit yearly payment (PENDING)
    PATCH /api/payments/subscription/<id>/decision/  Apex approve/reject
    POST  /api/payments/setup/submit/              submit setup fee (PENDING)

Submissions are authenticated-only (the owner app will call them later with
the tenant resolved from the caller's profile, or an explicit `tenant` id).
Only the platform admin decides.
"""

from decimal import Decimal, InvalidOperation

from rest_framework.response import Response
from rest_framework.views import APIView

from user.platform import require_platform_admin

from .models import SetupPayment, SubscriptionPayment, Tenant
from .platform_views import log, setup_payment_row
from .serializers import TenantSerializer
from .services import (
    CHANNELS,
    approve_subscription,
    create_subscription_submission,
    payment_row,
    reject_subscription,
)


def resolve_tenant(request):
    """Tenant the submission is for: explicit `tenant` id wins, else the
    caller's own tenant (owner profile)."""
    tenant_id = request.data.get("tenant")
    if tenant_id:
        return Tenant.objects.filter(pk=tenant_id).first()
    profile = getattr(request.user, "profile", None)
    if profile and profile.tenant_id:
        return profile.tenant
    return None


def read_submission(request):
    """(tenant, channel, transaction_ref, amount, years, error_response)."""
    tenant = resolve_tenant(request)
    if tenant is None:
        return None, None, None, None, None, Response(
            {"errors": {"message": "No tenant for this account."}}, status=400
        )
    transaction_ref = (
        request.data.get("transaction_ref") or request.data.get("transaction_id") or ""
    ).strip()
    if not transaction_ref:
        return None, None, None, None, None, Response(
            {"errors": {"message": "transaction_ref is required."}}, status=400
        )
    channel = (request.data.get("channel") or "OTHER").upper()
    if channel not in CHANNELS:
        return None, None, None, None, None, Response(
            {"errors": {"message": "channel must be TELEBIRR, BANK, CASH or OTHER."}},
            status=400,
        )
    try:
        amount = Decimal(str(request.data.get("amount") or "0"))
    except InvalidOperation:
        return None, None, None, None, None, Response(
            {"errors": {"message": "amount must be a number."}}, status=400
        )
    if amount <= 0:
        return None, None, None, None, None, Response(
            {"errors": {"message": "amount must be greater than 0."}}, status=400
        )
    years = int(request.data.get("years") or 1)
    years = max(1, min(years, 5))
    return tenant, channel, transaction_ref, amount, years, None


class SubscriptionSubmitView(APIView):
    """POST /api/payments/subscription/ — yearly payment, owner side.

    Creates a PENDING row; the tenant's subscription is NOT extended until
    a platform admin approves it. yearly billing only (1–5 years, default 1).
    """

    def post(self, request):
        tenant, channel, transaction_ref, amount, years, error = read_submission(request)
        if error is not None:
            return error
        payment = create_subscription_submission(
            request.user,
            tenant,
            channel,
            transaction_ref,
            amount,
            years,
            request.data.get("note", ""),
        )
        log(
            request.user,
            "SUBSCRIPTION_SUBMITTED",
            "subscription_payment",
            payment.pk,
            transaction_ref,
        )
        return Response(
            {
                "data": {
                    **payment_row(payment),
                    "tenant_expires_on": tenant.subscription_expires_on,
                    "tenant_state": tenant.subscription_state,
                }
            },
            status=201,
        )


class SubscriptionDecisionView(APIView):
    """PATCH /api/payments/subscription/<id>/decision/ — {decision, note}.

    APPROVED → extends tenant.subscription_expires_on by 365 × years from
    max(today, current expiry) and stamps the tenant's paid/transaction
    fields. REJECTED → records the decision only. Only PENDING rows can be
    decided; every decision is audit-logged.
    """

    @require_platform_admin
    def patch(self, request, pk):
        payment = SubscriptionPayment.objects.select_related("tenant").filter(pk=pk).first()
        if payment is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if payment.status != "PENDING":
            return Response(
                {"errors": {"message": "Payment already decided."}}, status=400
            )
        decision = request.data.get("decision")
        if decision not in {"APPROVED", "REJECTED"}:
            return Response(
                {"errors": {"message": "decision must be APPROVED or REJECTED."}},
                status=400,
            )
        note = (
            request.data.get("note")
            or request.data.get("decision_note")
            or request.data.get("reason")
            or ""
        ).strip()
        if decision == "REJECTED" and len(note) < 4:
            return Response(
                {
                    "errors": {
                        "message": "Explain the rejection — the wholesaler sees this note."
                    }
                },
                status=400,
            )
        if decision == "APPROVED":
            covers_until = approve_subscription(payment, request.user, note)
            action = "SUBSCRIPTION_APPROVED"
        else:
            reject_subscription(payment, request.user, note)
            covers_until = payment.covers_until
            action = "SUBSCRIPTION_REJECTED"
        log(
            request.user,
            action,
            "subscription_payment",
            payment.pk,
            payment.transaction_id,
            reason=note,
        )
        return Response(
            {
                "data": {
                    **payment_row(payment),
                    "covers_until": covers_until,
                    "tenant": TenantSerializer(payment.tenant).data,
                }
            }
        )


class SetupPaymentSubmitView(APIView):
    """POST /api/payments/setup/submit/ — onboarding fee, owner side.

    Creates a PENDING SetupPayment; the platform PATCH on
    /api/tenants/setup-payments/ approves or rejects it.
    """

    def post(self, request):
        from django.utils import timezone

        tenant = resolve_tenant(request)
        if tenant is None:
            return Response(
                {"errors": {"message": "No tenant for this account."}}, status=400
            )
        transaction_ref = (
            request.data.get("transaction_ref") or request.data.get("transaction_id") or ""
        ).strip()
        if not transaction_ref:
            return Response(
                {"errors": {"message": "transaction_ref is required."}}, status=400
            )
        channel = (request.data.get("channel") or "OTHER").upper()
        if channel not in CHANNELS:
            return Response(
                {"errors": {"message": "channel must be TELEBIRR, BANK, CASH or OTHER."}},
                status=400,
            )
        try:
            amount = Decimal(str(request.data.get("amount") or "0"))
        except InvalidOperation:
            return Response(
                {"errors": {"message": "amount must be a number."}}, status=400
            )
        if amount <= 0:
            return Response(
                {"errors": {"message": "amount must be greater than 0."}}, status=400
            )
        row = SetupPayment.objects.create(
            tenant=tenant,
            amount=amount,
            transaction_ref=transaction_ref,
            channel=channel,
            status="PENDING",
            note=request.data.get("note", ""),
            submitted_by=request.user if request.user.is_authenticated else None,
            submitted_at=timezone.now(),
        )
        log(request.user, "SETUP_SUBMITTED", "setup_payment", row.pk, tenant.name)
        return Response({"data": setup_payment_row(row)}, status=201)
