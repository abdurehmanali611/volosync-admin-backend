from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, F, Q, Sum
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


def log(actor, action, target_type="", target_id="", detail="", reason=""):
    AuditLog.objects.create(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        detail=detail,
        reason=reason,
    )


def truthy(value):
    return str(value).lower() in ("1", "true", "yes")


# Longest prefix first — `TENANT_SUSPEND` must win over `TENANT`.
AUDIT_PREFIXES = (
    ("CHAT", "chat"),
    ("STAFF_LOGIN", "staff"),
    ("OWNER", "staff"),
    ("PASSWORD", "staff"),
    ("MANAGER", "staff"),
    ("TENANT_SUSPEND", "access"),
    ("TENANT_UNSUSPEND", "access"),
    ("TENANT_BAN", "access"),
    ("TENANT_UNBAN", "access"),
    ("TENANT_DELETE", "access"),
    ("TENANT_RESTORE", "access"),
    ("TENANT_BILLING", "payments"),
    ("SUBSCRIPTION", "payments"),
    ("SETUP", "payments"),
    ("MODULE", "modules"),
    ("PRICING", "modules"),
    ("FEEDBACK", "feedback"),
    ("EXCEL", "imports"),
    ("TENANT", "tenants"),
    ("BRANCH", "tenants"),
)
AUDIT_CATEGORIES = {category for _, category in AUDIT_PREFIXES} | {"other"}


def audit_category(action):
    for prefix, category in AUDIT_PREFIXES:
        if (action or "").startswith(prefix):
            return category
    return "other"


def _limit(request, default=200, maximum=500):
    try:
        return max(1, min(int(request.query_params.get("limit") or default), maximum))
    except ValueError:
        return default


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
        "submitted_by": row.submitted_by.username if row.submitted_by else "",
        "submitted_at": row.submitted_at,
        "decided_by": row.decided_by.username if row.decided_by else "",
        "decided_at": row.decided_at,
        "decision_note": row.decision_note,
        "created_at": row.created_at,
    }


class FeedbackListView(APIView):
    """?status= OPEN|READ|RESOLVED · ?category= · ?search= · ?unread=true"""

    @require_platform_admin
    def get(self, request):
        params = request.query_params
        rows = Feedback.objects.select_related("tenant")
        status_filter = (params.get("status") or "").upper()
        if status_filter in {"OPEN", "READ", "RESOLVED"}:
            rows = rows.filter(status=status_filter)
        category = (params.get("category") or "").strip().upper()
        if category:
            rows = rows.filter(category=category)
        term = (params.get("search") or "").strip()
        if term:
            rows = rows.filter(
                Q(subject__icontains=term)
                | Q(body__icontains=term)
                | Q(contact__icontains=term)
                | Q(tenant__name__icontains=term)
            )
        if (params.get("unread") or "").lower() in ("1", "true", "yes"):
            rows = rows.filter(unread=True)
        limit = _limit(request)
        return Response(
            {
                "data": [
                    {
                        "id": row.id,
                        "tenant": row.tenant.name if row.tenant else "",
                        "tenant_id": row.tenant_id,
                        "subject": row.subject,
                        "body": row.body,
                        "category": row.category,
                        "status": row.status,
                        "contact": row.contact,
                        "unread": row.unread,
                        "created_at": row.created_at,
                    }
                    for row in rows[:limit]
                ],
                "meta": {
                    "total": rows.count(),
                    "unread": Feedback.objects.filter(unread=True).count(),
                },
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
    """?category= access|payments|modules|staff|chat|feedback|imports|tenants
    ?search= actor / action / target / detail / reason · ?limit · ?offset"""

    @require_platform_admin
    def get(self, request):
        rows = AuditLog.objects.select_related("actor")
        category = (request.query_params.get("category") or "").strip().lower()
        if category:
            prefixes = [
                prefix for prefix, name in AUDIT_PREFIXES if name == category
            ]
            if category == "other":
                known = [prefix for prefix, _ in AUDIT_PREFIXES]
                unclassified = Q(action__startswith=known[0])
                for prefix in known[1:]:
                    unclassified |= Q(action__startswith=prefix)
                rows = rows.exclude(unclassified)
            elif prefixes:
                narrowed = Q(action__startswith=prefixes[0])
                for prefix in prefixes[1:]:
                    narrowed |= Q(action__startswith=prefix)
                rows = rows.filter(narrowed)
            else:
                rows = rows.none()
        term = (request.query_params.get("search") or "").strip()
        if term:
            rows = rows.filter(
                Q(actor__username__icontains=term)
                | Q(action__icontains=term)
                | Q(target_type__icontains=term)
                | Q(target_id__icontains=term)
                | Q(detail__icontains=term)
                | Q(reason__icontains=term)
            )
        actor = (request.query_params.get("actor") or "").strip()
        if actor:
            rows = rows.filter(actor__username=actor)
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 200), 500))
            offset = max(0, int(request.query_params.get("offset") or 0))
        except ValueError:
            limit, offset = 200, 0
        total = rows.count()
        payload = [
            {
                "id": row.id,
                "actor": row.actor.username if row.actor else "system",
                "action": row.action,
                "category": audit_category(row.action),
                "target_type": row.target_type,
                "target_id": row.target_id,
                "detail": row.detail,
                "reason": row.reason,
                "at": row.at,
            }
            for row in rows[offset : offset + limit]
        ]
        return Response(
            {
                "data": payload,
                "meta": {"total": total, "categories": sorted(AUDIT_CATEGORIES)},
            }
        )


def _shift_month(day, delta):
    """The first day of the month `delta` months away from `day`."""
    from datetime import date

    index = day.year * 12 + (day.month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


class MonthlySignupsView(APIView):
    """Tenants created in one calendar month, with their setup-payment state.

    ?month=YYYY-MM (default: the current month) · ?months=N to return the
    trailing N months newest-first · ?search= name/TIN · ?owner= missing|present
    Mirrors the reference console's Signups page: one row per new wholesaler
    with the latest setup payment standing (PENDING when none was submitted).
    """

    @require_platform_admin
    def get(self, request):
        from django.utils import timezone

        from user.models import Profile

        params = request.query_params
        raw_month = (params.get("month") or "").strip()
        if raw_month:
            try:
                year, month = raw_month.split("-")
                start = timezone.localdate().replace(year=int(year), month=int(month), day=1)
            except (ValueError, TypeError):
                return Response(
                    {"errors": {"message": "month must look like 2026-11."}}, status=400
                )
        else:
            start = timezone.localdate().replace(day=1)
        try:
            window = max(1, min(int(params.get("months") or 1), 12))
        except ValueError:
            window = 1
        end = _shift_month(start, 1)
        start = _shift_month(start, -(window - 1))

        latest = {}
        for payment in SetupPayment.objects.filter(
            tenant__created_at__date__gte=start, tenant__created_at__date__lt=end
        ).order_by("created_at"):
            latest[payment.tenant_id] = payment

        tenants = Tenant.objects.filter(
            created_at__date__gte=start, created_at__date__lt=end
        ).select_related("sales_agent", "parent")
        term = (params.get("search") or "").strip()
        if term:
            tenants = tenants.filter(
                Q(name__icontains=term)
                | Q(tin__icontains=term)
                | Q(parent__name__icontains=term)
            )
        owner_tenant_ids = set(
            Profile.objects.filter(role="OWNER").values_list("tenant_id", flat=True)
        )
        want_owner = (params.get("owner") or "").strip().lower()
        if want_owner == "missing":
            tenants = tenants.exclude(pk__in=owner_tenant_ids)
        elif want_owner == "present":
            tenants = tenants.filter(pk__in=owner_tenant_ids)

        rows = []
        for tenant in tenants.order_by("-created_at"):
            payment = latest.get(tenant.pk)
            rows.append(
                {
                    "id": tenant.id,
                    "name": tenant.name,
                    "tin": tenant.tin,
                    "created_at": tenant.created_at,
                    "sales_agent": tenant.sales_agent.name if tenant.sales_agent else "",
                    "is_branch": tenant.parent_id is not None,
                    "parent": tenant.parent.name if tenant.parent else "",
                    "phone": tenant.phone,
                    "owner_present": tenant.pk in owner_tenant_ids,
                    "account_status": tenant.account_status,
                    "subscription_state": tenant.subscription_state,
                    "billing_state": tenant.billing_state,
                    "annual_fee": str(tenant.annual_fee),
                    "setup_status": payment.status if payment else "PENDING",
                    "setup_amount": str(payment.amount) if payment else None,
                    "setup_ref": payment.transaction_ref if payment else "",
                    "setup_submitted_by": payment.submitted_by.username
                    if payment and payment.submitted_by
                    else "",
                    "setup_decided_by": payment.decided_by.username
                    if payment and payment.decided_by
                    else "",
                    "module_inventory": tenant.module_inventory,
                    "module_hr": tenant.module_hr,
                    "sales_role_enabled": tenant.sales_role_enabled,
                }
            )
        return Response(
            {
                "data": rows,
                "meta": {
                    "month": f"{start.year}-{start.month:02d}",
                    "window_months": window,
                    "total": len(rows),
                },
            }
        )


class SetupPaymentListView(APIView):
    """Onboarding (setup) fee queue: list + approve/reject decision."""

    @require_platform_admin
    def get(self, request):
        rows = SetupPayment.objects.select_related(
            "tenant", "submitted_by", "decided_by"
        )
        status_filter = (request.query_params.get("status") or "").upper()
        if status_filter in {"PENDING", "APPROVED", "REJECTED"}:
            rows = rows.filter(status=status_filter)
        term = (request.query_params.get("search") or "").strip()
        if term:
            rows = rows.filter(
                Q(tenant__name__icontains=term)
                | Q(tenant__tin__icontains=term)
                | Q(transaction_ref__icontains=term)
            )
        limit = _limit(request, default=200)
        return Response(
            {"data": [setup_payment_row(row) for row in rows[:limit]], "meta": {"total": rows.count()}}
        )

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
        note = (
            request.data.get("decision_note")
            or request.data.get("note")
            or request.data.get("reason")
            or ""
        ).strip()
        if new_status == "REJECTED" and len(note) < 4:
            return Response(
                {"errors": {"message": "Explain the rejection — the wholesaler sees it."}},
                status=400,
            )
        from django.utils import timezone

        row.status = new_status
        row.decided_by = request.user
        row.decided_at = timezone.now()
        row.decision_note = note
        row.save(update_fields=["status", "decided_by", "decided_at", "decision_note"])
        log(
            request.user,
            "SETUP_" + row.status,
            "setup_payment",
            row.id,
            row.tenant.name,
            reason=note,
        )
        return Response({"data": setup_payment_row(row)})


class ModuleRequestListView(APIView):
    """Switch-on/switch-off requests: the review sheet compares current state,
    the requested state and the yearly fee the switch would produce.

    GET  ?status=PENDING|APPROVED|REJECTED · ?search= tenant/TIN
    PATCH {id, status|decision, review_note?, apply_fee?}
          APPROVED applies the switches (and reprices when apply_fee is set);
          REJECTED requires a review note.
    """

    MODULE_ATTRS = {
        "INVENTORY": "module_inventory",
        "HR": "module_hr",
        "SALES_ROLE": "sales_role_enabled",
    }

    def _row(self, row):
        return {
            "id": row.id,
            "tenant": row.tenant.name,
            "tenant_id": row.tenant_id,
            "tin": row.tenant.tin,
            "requested_modules": row.requested_modules,
            "current_modules": row.current_modules,
            "note": row.note,
            "status": row.status,
            "projected_annual_fee": str(row.projected_annual_fee)
            if row.projected_annual_fee is not None
            else None,
            "annual_fee": str(row.tenant.annual_fee),
            "review_note": row.review_note,
            "decided_by": row.decided_by.username if row.decided_by else "",
            "created_at": row.created_at,
            "decided_at": row.decided_at,
        }

    @require_platform_admin
    def get(self, request):
        rows = ModuleRequest.objects.select_related("tenant", "decided_by")
        status_filter = (request.query_params.get("status") or "").upper()
        if status_filter in {"PENDING", "APPROVED", "REJECTED"}:
            rows = rows.filter(status=status_filter)
        term = (request.query_params.get("search") or "").strip()
        if term:
            rows = rows.filter(
                Q(tenant__name__icontains=term) | Q(tenant__tin__icontains=term)
            )
        limit = _limit(request)
        return Response(
            {
                "data": [self._row(row) for row in rows[:limit]],
                "meta": {"total": rows.count()},
            }
        )

    @require_platform_admin
    def patch(self, request, pk=None):
        from django.utils import timezone

        from .pricing import suggest_annual_fee

        row_id = pk or request.data.get("id")
        row = ModuleRequest.objects.select_related("tenant").filter(pk=row_id).first()
        if row is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if row.status != "PENDING":
            return Response({"errors": {"message": "Request already decided."}}, status=400)
        new_status = (request.data.get("status") or request.data.get("decision") or "").upper()
        if new_status not in {"APPROVED", "REJECTED"}:
            return Response(
                {"errors": {"message": "decision must be APPROVED or REJECTED."}},
                status=400,
            )
        review_note = (
            request.data.get("review_note")
            or request.data.get("note")
            or request.data.get("reason")
            or ""
        ).strip()
        if new_status == "REJECTED" and len(review_note) < 4:
            return Response(
                {"errors": {"message": "Explain the decision — the wholesaler sees it."}},
                status=400,
            )

        tenant = row.tenant
        projected = None
        switched = []
        if new_status == "APPROVED":
            wanted = {
                part.strip().upper()
                for part in (row.requested_modules or "").split(",")
                if part.strip()
            }
            wanted -= set(Tenant.REQUIRED_MODULES)
            for code, attr in self.MODULE_ATTRS.items():
                if code in wanted and not getattr(tenant, attr):
                    setattr(tenant, attr, True)
                    switched.append(attr)
            disabled = {
                part.strip().upper()
                for part in (request.data.get("disable_modules") or "").split(",")
                if part.strip()
            }
            for code, attr in self.MODULE_ATTRS.items():
                if code in disabled and getattr(tenant, attr):
                    setattr(tenant, attr, False)
                    switched.append(attr)

        projected = suggest_annual_fee(tenant)
        if truthy(request.data.get("apply_fee")) and new_status == "APPROVED":
            if projected is None:
                return Response(
                    {"errors": {"message": "No default active pricing plan to price from."}},
                    status=400,
                )
            tenant.annual_fee = projected
            tenant.fees_manually_set = False

        with transaction.atomic():
            if switched or (
                truthy(request.data.get("apply_fee")) and new_status == "APPROVED"
            ):
                tenant.save(
                    update_fields=list(dict.fromkeys(switched + ["annual_fee", "fees_manually_set"]))
                )
            row.status = new_status
            row.review_note = review_note
            row.decided_by = request.user
            row.decided_at = timezone.now()
            row.projected_annual_fee = projected
            row.save(
                update_fields=[
                    "status",
                    "review_note",
                    "decided_by",
                    "decided_at",
                    "projected_annual_fee",
                ]
            )
        log(
            request.user,
            "MODULE_" + row.status,
            "module_request",
            row.id,
            f"{tenant.name} · {row.requested_modules}"
            + (f" · {sorted(set(switched))}" if switched else ""),
            reason=review_note,
        )
        return Response({"data": self._row(row)})


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


class PricingPreviewView(APIView):
    """What a wholesaler would pay, without saving anything.

    GET  ?tenant=<id>&plan=<id>       — preview a real tenant's current switches
    POST {module_inventory?, module_hr?, sales_role_enabled?, tenant?, plan?}
    Returns the fee lines, the projected yearly total and the delta against the
    tenant's stored annual_fee. `plan` defaults to the default active plan.
    """

    @require_platform_admin
    def _payload(self, request):
        data = request.data if request.method == "POST" else request.query_params
        plan = None
        plan_id = str(data.get("plan") or data.get("pricing_plan") or "").strip()
        if plan_id and plan_id != "default":
            plan = PricingPlan.objects.filter(pk=plan_id).first()
            if plan is None:
                return None, None, Response(
                    {"errors": {"message": "That pricing plan does not exist."}}, status=404
                )
        plan = plan or default_plan()
        if plan is None:
            return None, None, Response(
                {"errors": {"message": "No default active pricing plan exists."}}, status=400
            )

        tenant = None
        tenant_id = str(data.get("tenant") or "").strip()
        if tenant_id and tenant_id.lower() != "null":
            tenant = Tenant.objects.filter(pk=tenant_id).first()
            if tenant is None:
                return plan, None, Response(
                    {"errors": {"message": "That wholesaler does not exist."}}, status=404
                )

        def flag(key, default=False):
            if key not in data:
                return getattr(tenant, key, default) if tenant else default
            return truthy(data[key])

        flags = {
            "module_inventory": flag("module_inventory"),
            "module_hr": flag("module_hr"),
            "sales_role_enabled": flag("sales_role_enabled"),
        }
        lines = [
            {"code": "YEARLY_BASE", "name": "Yearly base", "amount": str(plan.yearly_base_fee), "applied": True},
            {
                "code": "INVENTORY",
                "name": "Inventory module",
                "amount": str(plan.inventory_yearly_fee),
                "applied": flags["module_inventory"],
            },
            {
                "code": "HR",
                "name": "HR module",
                "amount": str(plan.hr_yearly_fee),
                "applied": flags["module_hr"],
            },
            {
                "code": "SALES_ROLE",
                "name": "Sales role",
                "amount": str(plan.sales_role_yearly_fee),
                "applied": flags["sales_role_enabled"],
            },
        ]
        total = sum(
            (Decimal(line["amount"]) for line in lines if line["applied"]), Decimal("0.00")
        )
        current = tenant.annual_fee if tenant else None
        return (
            plan,
            {
                "plan": PricingPlanSerializer(plan).data,
                "lines": lines,
                "total_annual_fee": str(total),
                "setup_fee": str(plan.setup_fee),
                "current_annual_fee": str(current) if current is not None else None,
                "delta": str(total - current) if current is not None else None,
                "flags": flags,
                "tenant": tenant.name if tenant else None,
                "fees_manually_set": tenant.fees_manually_set if tenant else False,
            },
            None,
        )

    @require_platform_admin
    def get(self, request):
        _plan, payload, error = self._payload(request)
        return error or Response({"data": payload})

    @require_platform_admin
    def post(self, request):
        _plan, payload, error = self._payload(request)
        return error or Response({"data": payload})


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
