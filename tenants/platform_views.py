from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, F, Sum
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from inventory.models import StockLot
from user.platform import require_platform_admin

from .models import (
    AuditLog,
    Feedback,
    ModuleRequest,
    PricingPlan,
    SetupPayment,
    SubscriptionPayment,
    Tenant,
)
from .pricing import default_plan
from .serializers import PricingPlanSerializer

CHANNELS = {"TELEBIRR", "BANK", "CASH", "OTHER"}


def log(actor, action, target_type="", target_id="", detail=""):
    AuditLog.objects.create(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        detail=detail,
    )


def truthy(value):
    return str(value).lower() in ("1", "true", "yes")


def setup_payment_row(row):
    return {
        "id": row.id,
        "tenant": row.tenant.name,
        "tenant_id": row.tenant_id,
        "amount": str(row.amount.quantize(Decimal("0.01"))),
        "transaction_ref": row.transaction_ref,
        "channel": row.channel,
        "status": row.status,
        "note": row.note,
        "decided_by": row.decided_by.username if row.decided_by else "",
        "decided_at": row.decided_at,
        "decision_note": row.decision_note,
        "created_at": row.created_at,
    }


class FeedbackListView(APIView):
    @require_platform_admin
    def get(self, request):
        rows = Feedback.objects.select_related("tenant")[:200]
        return Response(
            {
                "data": [
                    {
                        "id": row.id,
                        "tenant": row.tenant.name if row.tenant else "",
                        "subject": row.subject,
                        "body": row.body,
                        "category": row.category,
                        "status": row.status,
                        "contact": row.contact,
                        "unread": row.unread,
                        "created_at": row.created_at,
                    }
                    for row in rows
                ]
            }
        )

    @require_platform_admin
    def post(self, request):
        tenant = Tenant.objects.filter(pk=request.data.get("tenant")).first()
        row = Feedback.objects.create(
            tenant=tenant,
            subject=request.data.get("subject", "Untitled"),
            body=request.data.get("body", ""),
            category=request.data.get("category", "GENERAL"),
            contact=request.data.get("contact", ""),
        )
        log(request.user, "FEEDBACK_CREATED", "feedback", row.id, row.subject)
        return Response({"data": {"id": row.id}}, status=201)

    @require_platform_admin
    def patch(self, request):
        row = Feedback.objects.filter(pk=request.data.get("id")).first()
        if row is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.status = request.data.get("status", row.status)
        row.unread = row.status == "OPEN"
        row.save(update_fields=["status", "unread"])
        log(request.user, "FEEDBACK_" + row.status, "feedback", row.id, row.subject)
        return Response({"data": {"id": row.id, "status": row.status}})


class AuditLogListView(APIView):
    @require_platform_admin
    def get(self, request):
        rows = AuditLog.objects.select_related("actor")[:200]
        return Response(
            {
                "data": [
                    {
                        "id": row.id,
                        "actor": row.actor.username if row.actor else "system",
                        "action": row.action,
                        "target_type": row.target_type,
                        "target_id": row.target_id,
                        "detail": row.detail,
                        "at": row.at,
                    }
                    for row in rows
                ]
            }
        )


class MonthlySignupsView(APIView):
    """Tenants created this calendar month, with their setup-payment state.

    Mirrors the reference console's Signups page: one row per new wholesaler
    with the latest setup payment standing (PENDING when none was submitted).
    """

    @require_platform_admin
    def get(self, request):
        from django.utils import timezone

        today = timezone.localdate()
        start = today.replace(day=1)
        latest = {}
        for payment in SetupPayment.objects.filter(
            tenant__created_at__date__gte=start
        ).order_by("created_at"):
            latest[payment.tenant_id] = payment

        rows = []
        for tenant in (
            Tenant.objects.filter(created_at__date__gte=start)
            .select_related("sales_agent")
            .order_by("-created_at")
        ):
            payment = latest.get(tenant.pk)
            rows.append(
                {
                    "id": tenant.id,
                    "name": tenant.name,
                    "tin": tenant.tin,
                    "created_at": tenant.created_at,
                    "sales_agent": tenant.sales_agent.name if tenant.sales_agent else "",
                    "is_branch": tenant.parent_id is not None,
                    "subscription_state": tenant.subscription_state,
                    "setup_status": payment.status if payment else "PENDING",
                    "setup_amount": str(payment.amount) if payment else None,
                    "setup_ref": payment.transaction_ref if payment else "",
                    "module_inventory": tenant.module_inventory,
                    "module_hr": tenant.module_hr,
                }
            )
        return Response({"data": rows})


class SetupPaymentListView(APIView):
    """Onboarding (setup) fee queue: list + approve/reject decision."""

    @require_platform_admin
    def get(self, request):
        rows = SetupPayment.objects.select_related("tenant", "decided_by")[:200]
        return Response({"data": [setup_payment_row(row) for row in rows]})

    @require_platform_admin
    def patch(self, request):
        """{status|decision: APPROVED|REJECTED, note?} on a PENDING row.

        Records decided_by/decided_at/decision_note and audit-logs the call.
        Approving a setup payment only records the fee — tenant activation is
        not tied to it.
        """
        row = SetupPayment.objects.filter(pk=request.data.get("id")).first()
        if row is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if row.status != "PENDING":
            return Response(
                {"errors": {"message": "Payment already decided."}}, status=400
            )
        new_status = request.data.get("status") or request.data.get("decision")
        if new_status not in {"APPROVED", "REJECTED"}:
            return Response(
                {"errors": {"message": "decision must be APPROVED or REJECTED."}},
                status=400,
            )
        from django.utils import timezone

        row.status = new_status
        row.decided_by = request.user
        row.decided_at = timezone.now()
        row.decision_note = request.data.get(
            "decision_note", request.data.get("note", "")
        )
        row.save(update_fields=["status", "decided_by", "decided_at", "decision_note"])
        log(request.user, "SETUP_" + row.status, "setup_payment", row.id, row.tenant.name)
        return Response({"data": {"id": row.id, "status": row.status}})


class ModuleRequestListView(APIView):
    @require_platform_admin
    def get(self, request):
        rows = ModuleRequest.objects.select_related("tenant")[:200]
        return Response(
            {
                "data": [
                    {
                        "id": row.id,
                        "tenant": row.tenant.name,
                        "tenant_id": row.tenant_id,
                        "requested_modules": row.requested_modules,
                        "note": row.note,
                        "status": row.status,
                        "created_at": row.created_at,
                        "decided_at": row.decided_at,
                    }
                    for row in rows
                ]
            }
        )

    @require_platform_admin
    def patch(self, request):
        from django.utils import timezone

        row = ModuleRequest.objects.filter(pk=request.data.get("id")).first()
        if row is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.status = request.data.get("status", row.status)
        row.decided_at = timezone.now()
        row.save(update_fields=["status", "decided_at"])
        if row.status == "APPROVED":
            tenant = row.tenant
            wanted = {part.strip().upper() for part in row.requested_modules.split(",")}
            # Credentials is always on — never an approvable request.
            wanted -= set(Tenant.REQUIRED_MODULES)
            if "INVENTORY" in wanted:
                tenant.module_inventory = True
            if "HR" in wanted:
                tenant.module_hr = True
            tenant.save(update_fields=["module_inventory", "module_hr"])
        log(request.user, "MODULE_" + row.status, "module_request", row.id, row.requested_modules)
        return Response({"data": {"id": row.id, "status": row.status}})


FEE_FIELDS = (
    "setup_fee",
    "yearly_base_fee",
    "inventory_yearly_fee",
    "hr_yearly_fee",
    "sales_role_yearly_fee",
)


def read_fees(data):
    """Coerce the fee fields present in `data`. Returns (fees, error)."""
    fees = {}
    for key in FEE_FIELDS:
        if key in data:
            try:
                fees[key] = Decimal(str(data[key] or "0"))
            except InvalidOperation:
                return None, f"{key} must be a number."
    return fees, None


class PricingPlanListCreateView(APIView):
    """Pricing catalog (yearly only — no quarterly fee exists anywhere)."""

    @require_platform_admin
    def get(self, request):
        plans = PricingPlan.objects.all()
        return Response({"data": PricingPlanSerializer(plans, many=True).data})

    @require_platform_admin
    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"errors": {"message": "Plan name is required."}}, status=400)
        if PricingPlan.objects.filter(name=name).exists():
            return Response({"errors": {"message": "That plan name is taken."}}, status=400)
        fees, error = read_fees(request.data)
        if error:
            return Response({"errors": {"message": error}}, status=400)
        is_default = truthy(request.data.get("is_default"))
        active = request.data.get("active", True)
        active = truthy(active) if not isinstance(active, bool) else active
        with transaction.atomic():
            if is_default:
                PricingPlan.objects.filter(is_default=True).update(is_default=False)
            plan = PricingPlan.objects.create(
                name=name,
                is_default=is_default,
                active=active,
                **fees,
            )
        log(request.user, "PRICING_PLAN_CREATED", "pricing_plan", plan.pk, plan.name)
        return Response({"data": PricingPlanSerializer(plan).data}, status=201)


class PricingPlanDetailView(APIView):
    @require_platform_admin
    def get(self, request, pk):
        plan = PricingPlan.objects.filter(pk=pk).first()
        if plan is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        return Response({"data": PricingPlanSerializer(plan).data})

    @require_platform_admin
    def patch(self, request, pk):
        plan = PricingPlan.objects.filter(pk=pk).first()
        if plan is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if "name" in request.data:
            name = (request.data.get("name") or "").strip()
            if not name:
                return Response(
                    {"errors": {"message": "Plan name is required."}}, status=400
                )
            if PricingPlan.objects.filter(name=name).exclude(pk=plan.pk).exists():
                return Response(
                    {"errors": {"message": "That plan name is taken."}}, status=400
                )
            plan.name = name
        fees, error = read_fees(request.data)
        if error:
            return Response({"errors": {"message": error}}, status=400)
        for key, value in fees.items():
            setattr(plan, key, value)
        if "active" in request.data:
            plan.active = (
                request.data["active"]
                if isinstance(request.data["active"], bool)
                else truthy(request.data["active"])
            )
        if "is_default" in request.data:
            plan.is_default = (
                request.data["is_default"]
                if isinstance(request.data["is_default"], bool)
                else truthy(request.data["is_default"])
            )
        with transaction.atomic():
            if plan.is_default:
                PricingPlan.objects.filter(is_default=True).exclude(pk=plan.pk).update(
                    is_default=False
                )
            plan.save()
        log(request.user, "PRICING_PLAN_UPDATED", "pricing_plan", plan.pk, plan.name)
        return Response({"data": PricingPlanSerializer(plan).data})

    @require_platform_admin
    def delete(self, request, pk):
        plan = PricingPlan.objects.filter(pk=pk).first()
        if plan is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        plan.delete()
        log(request.user, "PRICING_PLAN_DELETED", "pricing_plan", pk, "")
        return Response({"data": {"id": int(pk), "deleted": True}})


class ModulesCatalogView(APIView):
    """Available platform modules with the fee from the default active plan."""

    @require_platform_admin
    def get(self, request):
        plan = default_plan()
        available = [
            {
                # Credentials is the basic layer: always on for every tenant.
                # It is never requestable (ModuleRequest DECISIONS) and never
                # billable, so it carries no fee.
                "code": "CREDENTIALS",
                "name": "Credentials",
                "description": "Login accounts, roles and permissions — included with every plan.",
                "yearly_fee": None,
                "always_on": True,
            },
            {
                "code": "INVENTORY",
                "name": "Inventory",
                "description": "Voucher-lot stock: store, finance and manager approvals, promotions, sales.",
                "yearly_fee": plan.inventory_yearly_fee if plan else None,
            },
            {
                "code": "HR",
                "name": "HR Module",
                "description": "Employees, leave, attendance, payroll workflows and the emp/ats companions.",
                "yearly_fee": plan.hr_yearly_fee if plan else None,
            },
        ]
        payload = [
            {
                **module,
                "yearly_fee": str(module["yearly_fee"])
                if module["yearly_fee"] is not None
                else None,
                # A module is commercially available while the default active
                # pricing plan exists (fees cannot be quoted without it).
                "active_for_platform": plan is not None and plan.active,
                "always_on": module.get("always_on", False),
                "requestable": not module.get("always_on", False),
            }
            for module in available
        ]
        return Response(
            {
                "data": {
                    "modules": payload,
                    "plan": PricingPlanSerializer(plan).data if plan else None,
                    "sales_role_enabled_fee": str(plan.sales_role_yearly_fee)
                    if plan
                    else None,
                }
            }
        )


class PlatformReportsView(APIView):
    """Per-tenant performance rows + totals (client-side export)."""

    @require_platform_admin
    def get(self, request):
        from user.models import Profile

        staff_counts = {
            row["tenant_id"]: row["c"]
            for row in Profile.objects.filter(tenant__isnull=False)
            .values("tenant_id")
            .annotate(c=Count("id"))
        }
        lot_rows = {
            row["tenant_id"]: row
            for row in StockLot.objects.values("tenant_id")
            .annotate(lots=Count("id"), value=Sum(F("remaining_quantity") * F("unit_cost")))
        }
        payment_counts = {
            row["tenant_id"]: row["c"]
            for row in SubscriptionPayment.objects.values("tenant_id").annotate(
                c=Count("id")
            )
        }

        rows = []
        totals = {
            "tenants": 0,
            "staff": 0,
            "lots": 0,
            "stock_value": Decimal("0.00"),
            "annual_fee": Decimal("0.00"),
            "payments": 0,
        }
        for tenant in Tenant.objects.all():
            lot = lot_rows.get(tenant.pk) or {}
            stock_value = lot.get("value") or Decimal("0.00")
            lot_count = lot.get("lots") or 0
            staff_count = staff_counts.get(tenant.pk, 0)
            payments_count = payment_counts.get(tenant.pk, 0)
            rows.append(
                {
                    "id": tenant.id,
                    "tenant": tenant.name,
                    "tin": tenant.tin,
                    "is_branch": tenant.parent_id is not None,
                    "state": tenant.subscription_state,
                    "annual_fee": str(tenant.annual_fee),
                    "staff_count": staff_count,
                    "lot_count": lot_count,
                    "stock_value": str(stock_value),
                    "payments_count": payments_count,
                }
            )
            totals["tenants"] += 1
            totals["staff"] += staff_count
            totals["lots"] += lot_count
            totals["stock_value"] += stock_value
            totals["annual_fee"] += tenant.annual_fee
            totals["payments"] += payments_count
        totals["stock_value"] = str(totals["stock_value"])
        totals["annual_fee"] = str(totals["annual_fee"])
        return Response({"data": {"rows": rows, "totals": totals}})
