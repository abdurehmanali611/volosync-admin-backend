"""Apex console operations the VoloSync app never exposes to tenants.

Access control, billing state, module switches, the per-tenant reporting
snapshot, wholesalers with no owner login and single-staff login revocation.
Every write here is audit-logged; destructive actions take a reason, which is
stored on the row AND on the audit entry.
"""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Count, F, Q, Sum
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from approvals.models import Approval
from assets.models import Asset
from inventory.models import (
    Item,
    RegistrationRequest,
    StockLot,
    StockMovement,
    StockOutRequest,
)
from sales.models import SalesAgent, SalesSettlement
from user.platform import require_platform_admin

from .models import (
    ChatMessage,
    ChatThread,
    ModuleRequest,
    SetupPayment,
    SubscriptionPayment,
    Tenant,
)
from .platform_views import log, truthy
from .pricing import default_plan, suggest_annual_fee
from .serializers import TenantSerializer

ZERO = Decimal("0.00")

# Every access action maps to the account_status it leaves behind, plus the
# field prefix that carries its reason/actor/timestamp trail.
ACCESS_ACTIONS = {
    "suspend": ("SUSPENDED", "suspended"),
    "ban": ("BANNED", "banned"),
    "delete": ("DELETED", "deleted"),
}
CLEAR_ACTIONS = {"unsuspend": "SUSPENDED", "unban": "BANNED", "restore": None}


def money(value):
    return str((value or ZERO).quantize(Decimal("0.01")))


def status_counts(queryset):
    """{STATUS: count} for any queryset that has a `status` column."""
    return {
        row["status"]: row["c"]
        for row in queryset.values("status").annotate(c=Count("id"))
    }


def access_payload(tenant):
    return {
        "account_status": tenant.account_status,
        "is_restricted": tenant.is_restricted,
        "suspended_reason": tenant.suspended_reason,
        "suspended_at": tenant.suspended_at,
        "suspended_by": tenant.suspended_by.username if tenant.suspended_by else "",
        "banned_reason": tenant.banned_reason,
        "banned_at": tenant.banned_at,
        "banned_by": tenant.banned_by.username if tenant.banned_by else "",
        "deleted_reason": tenant.deleted_reason,
        "deleted_at": tenant.deleted_at,
        "deleted_by": tenant.deleted_by.username if tenant.deleted_by else "",
    }


def billing_payload(tenant, plan=None):
    """The billing tile set: state, countdown, fee and how the fee was set."""
    plan = plan if plan is not None else default_plan()
    suggested = suggest_annual_fee(tenant, plan)
    return {
        "billing_state": tenant.billing_state,
        "subscription_state": tenant.subscription_state,
        "annual_fee": money(tenant.annual_fee),
        "suggested_annual_fee": money(suggested) if suggested is not None else None,
        "fees_manually_set": tenant.fees_manually_set,
        "is_illustration": tenant.is_illustration,
        "billing_hold": tenant.billing_hold,
        "billing_hold_note": tenant.billing_hold_note,
        "billing_hold_set_at": tenant.billing_hold_set_at,
        "free_trial_ends_on": tenant.free_trial_ends_on,
        "trial_days_left": tenant.trial_days_left,
        "days_to_renewal": tenant.days_to_renewal,
        "next_renewal_date": tenant.subscription_expires_on,
        "subscription_paid_on": tenant.subscription_paid_on,
        "subscription_transaction_id": tenant.subscription_transaction_id,
        "plan": plan.name if plan else None,
    }


def module_flags(tenant, plan=None):
    """Per-module state + the yearly-fee breakdown for this tenant."""
    plan = plan if plan is not None else default_plan()

    def fee(value):
        return money(value) if value is not None else None

    lines = [
        {
            "code": "CREDENTIALS",
            "name": "Credentials",
            "always_on": True,
            "enabled": True,
            "yearly_fee": None,
        },
        {
            "code": "INVENTORY",
            "name": "Inventory",
            "always_on": False,
            "enabled": tenant.module_inventory,
            "yearly_fee": fee(plan.inventory_yearly_fee if plan else None),
        },
        {
            "code": "HR",
            "name": "HR Module",
            "always_on": False,
            "enabled": tenant.module_hr,
            "yearly_fee": fee(plan.hr_yearly_fee if plan else None),
        },
        {
            "code": "SALES_ROLE",
            "name": "Sales role",
            "always_on": False,
            "enabled": tenant.sales_role_enabled,
            "yearly_fee": fee(plan.sales_role_yearly_fee if plan else None),
        },
    ]
    return {
        "modules": lines,
        "enabled": [line["code"] for line in lines if line["enabled"]],
        "plan": plan.name if plan else None,
        "projected_annual_fee": money(suggest_annual_fee(tenant, plan))
        if plan
        else None,
    }


def staff_row(profile):
    """One staff line — the shape both the tenant Staff tab and the global
    staff list serve, so the console can render either without branching."""
    user = profile.user
    return {
        "id": user.id,
        "profile_id": profile.id,
        "username": user.username,
        "name": user.get_full_name() or user.username,
        "role": profile.role,
        "tenant": profile.tenant.name if profile.tenant else "",
        "tenant_id": profile.tenant_id,
        # The console's user watchlist and deep links key on the TIN, and the
        # company/branch axis decides which filter tab a row belongs to.
        "tin": profile.tenant.tin if profile.tenant else "",
        "is_branch": bool(profile.tenant.parent_id) if profile.tenant else False,
        "created_at": profile.created_at,
        "phone": profile.phone,
        "last_login": user.last_login,
        "must_change_password": profile.must_change_password,
        "is_active": user.is_active,
        "login_disabled": profile.login_disabled,
        "login_disabled_reason": profile.login_disabled_reason,
        "login_disabled_at": profile.login_disabled_at,
        "login_disabled_by": profile.login_disabled_by.username
        if profile.login_disabled_by
        else "",
    }


def disable_staff(tenant, actor, reason):
    """Switch every staff login of one tenant off. Returns the row count."""
    from user.models import Profile

    return Profile.objects.filter(tenant=tenant, login_disabled=False).update(
        login_disabled=True,
        login_disabled_reason=reason,
        login_disabled_at=timezone.now(),
        login_disabled_by=actor,
    )


def enable_staff(tenant):
    from user.models import Profile

    return Profile.objects.filter(tenant=tenant, login_disabled=True).update(
        login_disabled=False,
        login_disabled_reason="",
        login_disabled_at=None,
        login_disabled_by=None,
    )


class TenantAccessView(APIView):
    """POST /api/tenants/<pk>/access/ {action, reason?, disable_staff?}

    suspend | unsuspend | ban | unban | delete | restore — a soft delete only:
    `DELETED` hides the wholesaler, no row is ever destroyed here.
    suspend/ban/delete need a reason; ban and delete also revoke the tenant's
    staff logins unless the operator opts out.
    """

    @require_platform_admin
    def get(self, request, pk):
        tenant = Tenant.objects.filter(pk=pk).first()
        if tenant is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        return Response({"data": access_payload(tenant)})

    @require_platform_admin
    def post(self, request, pk):
        tenant = Tenant.objects.filter(pk=pk).first()
        if tenant is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        action = (request.data.get("action") or "").strip().lower()
        if action not in ACCESS_ACTIONS and action not in CLEAR_ACTIONS:
            return Response(
                {
                    "errors": {
                        "message": "action must be suspend, unsuspend, ban, unban, delete or restore."
                    }
                },
                status=400,
            )
        reason = (request.data.get("reason") or "").strip()
        if action in ACCESS_ACTIONS and len(reason) < 4:
            return Response(
                {"errors": {"message": "A reason of at least 4 characters is required."}},
                status=400,
            )
        now = timezone.now()
        staff_revoked = staff_restored = 0

        if action in ACCESS_ACTIONS:
            state, prefix = ACCESS_ACTIONS[action]
            tenant.account_status = state
            setattr(tenant, f"{prefix}_reason", reason)
            setattr(tenant, f"{prefix}_at", now)
            setattr(tenant, f"{prefix}_by", request.user)
            tenant.save(
                update_fields=[
                    "account_status",
                    f"{prefix}_reason",
                    f"{prefix}_at",
                    f"{prefix}_by",
                ]
            )
            if truthy(request.data.get("disable_staff", action != "suspend")):
                staff_revoked = disable_staff(tenant, request.user, reason)
        else:
            required_state = CLEAR_ACTIONS[action]
            if required_state and tenant.account_status != required_state:
                return Response(
                    {
                        "errors": {
                            "message": f"This wholesaler is not {required_state.lower()}."
                        }
                    },
                    status=400,
                )
            if action == "restore" and tenant.account_status == "ACTIVE":
                return Response(
                    {"errors": {"message": "This wholesaler is already active."}}, status=400
                )
            tenant.account_status = "ACTIVE"
            tenant.save(update_fields=["account_status"])
            # The restriction trail stays on the row — only the state clears.
            if truthy(request.data.get("enable_staff", True)):
                staff_restored = enable_staff(tenant)

        log(
            request.user,
            "TENANT_" + action.upper(),
            "tenant",
            tenant.pk,
            f"{tenant.name} · {reason}" if reason else tenant.name,
            reason=reason,
        )
        data = {
            **access_payload(tenant),
            "staff_logins_revoked": staff_revoked,
            "staff_logins_restored": staff_restored,
            "tenant": TenantSerializer(tenant).data,
        }
        return Response({"data": data})


class TenantBillingView(APIView):
    """GET/PATCH /api/tenants/<pk>/billing/ — illustration, hold, trial, fee.

    PATCH accepts any of: is_illustration, billing_hold, billing_hold_note,
    free_trial_days (1-90, `null`/0 clears the trial), annual_fee (manual,
    stamps fees_manually_set), apply_fee (reprice from the default plan).
    """

    @require_platform_admin
    def get(self, request, pk):
        tenant = Tenant.objects.filter(pk=pk).first()
        if tenant is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        pending = SubscriptionPayment.objects.filter(
            tenant=tenant, status="PENDING"
        ).count()
        approved = SubscriptionPayment.objects.filter(tenant=tenant, status="APPROVED")
        return Response(
            {
                "data": {
                    **billing_payload(tenant),
                    "pending_subscription_payments": pending,
                    "approved_payments": approved.count(),
                    "paid_revenue": money(
                        approved.aggregate(total=Sum("amount"))["total"] or ZERO
                    ),
                    "last_payment": approved.order_by("-paid_on", "-id").first().paid_on
                    if approved.exists()
                    else None,
                    "setup_due": SetupPayment.objects.filter(
                        tenant=tenant, status="PENDING"
                    ).exists(),
                }
            }
        )

    @require_platform_admin
    def patch(self, request, pk):
        tenant = Tenant.objects.filter(pk=pk).first()
        if tenant is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        fields = []
        if "is_illustration" in request.data:
            tenant.is_illustration = truthy(request.data["is_illustration"])
            fields.append("is_illustration")

        if "billing_hold" in request.data:
            want_hold = truthy(request.data["billing_hold"])
            if want_hold and not tenant.billing_hold and len(
                (request.data.get("billing_hold_note") or "").strip()
            ) < 4:
                return Response(
                    {"errors": {"message": "Explain why renewals are on hold."}}, status=400
                )
            tenant.billing_hold = want_hold
            tenant.billing_hold_note = (
                (request.data.get("billing_hold_note") or "").strip()
                if want_hold
                else ""
            )
            tenant.billing_hold_set_at = timezone.now() if want_hold else None
            fields += ["billing_hold", "billing_hold_note", "billing_hold_set_at"]

        if "free_trial_days" in request.data or "free_trial_ends_on" in request.data:
            raw = request.data.get("free_trial_days", request.data.get("free_trial_ends_on"))
            if raw in (None, "", 0, "0", "null", "false", False):
                tenant.free_trial_ends_on = None
            else:
                try:
                    days = int(raw)
                except (TypeError, ValueError):
                    return Response(
                        {"errors": {"message": "free_trial_days must be a whole number."}},
                        status=400,
                    )
                if not 1 <= days <= 90:
                    return Response(
                        {"errors": {"message": "A free trial is 1 to 90 days."}}, status=400
                    )
                tenant.free_trial_ends_on = timezone.localdate() + timedelta(days=days)
            fields.append("free_trial_ends_on")

        if "annual_fee" in request.data:
            try:
                tenant.annual_fee = Decimal(str(request.data["annual_fee"] or "0"))
            except InvalidOperation:
                return Response(
                    {"errors": {"message": "annual_fee must be a number."}}, status=400
                )
            tenant.fees_manually_set = True
            fields += ["annual_fee", "fees_manually_set"]

        applied = False
        if truthy(request.data.get("apply_fee")):
            suggested = suggest_annual_fee(tenant)
            if suggested is None:
                return Response(
                    {"errors": {"message": "No default active pricing plan to price from."}},
                    status=400,
                )
            tenant.annual_fee = suggested
            tenant.fees_manually_set = False
            fields += ["annual_fee", "fees_manually_set"]
            applied = True

        if not fields:
            return Response({"errors": {"message": "Nothing to update."}}, status=400)
        tenant.save(update_fields=list(dict.fromkeys(fields)))
        log(
            request.user,
            "TENANT_BILLING_UPDATED",
            "tenant",
            tenant.pk,
            f"{tenant.name} · {sorted(set(fields))}",
        )
        payload = billing_payload(tenant)
        payload["fee_applied"] = applied
        payload["tenant"] = TenantSerializer(tenant).data
        return Response({"data": payload})


class TenantModulesView(APIView):
    """GET/PATCH /api/tenants/<pk>/modules/ — the module switches and reprice.

    PATCH {module_inventory?, module_hr?, sales_role_enabled?, apply_fee?,
    sync_branches?}. `sync_branches` pushes the same switches onto every
    branch of this company.
    """

    @require_platform_admin
    def get(self, request, pk):
        tenant = Tenant.objects.filter(pk=pk).first()
        if tenant is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        branches = [
            {
                "id": branch.id,
                "name": branch.name,
                "tin": branch.tin,
                **{
                    "module_inventory": branch.module_inventory,
                    "module_hr": branch.module_hr,
                    "sales_role_enabled": branch.sales_role_enabled,
                },
            }
            for branch in tenant.branches.only(
                "id", "name", "tin", "module_inventory", "module_hr", "sales_role_enabled"
            )
        ]
        pending = ModuleRequest.objects.filter(tenant=tenant, status="PENDING")
        return Response(
            {
                "data": {
                    **module_flags(tenant),
                    "annual_fee": money(tenant.annual_fee),
                    "requests": [
                        {
                            "id": row.id,
                            "requested_modules": row.requested_modules,
                            "current_modules": row.current_modules,
                            "projected_annual_fee": money(row.projected_annual_fee)
                            if row.projected_annual_fee is not None
                            else None,
                            "note": row.note,
                            "status": row.status,
                            "created_at": row.created_at,
                        }
                        for row in pending
                    ],
                    "branches": branches,
                }
            }
        )

    @require_platform_admin
    def patch(self, request, pk):
        tenant = Tenant.objects.filter(pk=pk).first()
        if tenant is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        switches = {
            "module_inventory": "module_inventory",
            "module_hr": "module_hr",
            "sales_role_enabled": "sales_role_enabled",
        }
        fields = []
        for key, attr in switches.items():
            if key in request.data:
                setattr(tenant, attr, truthy(request.data[key]))
                fields.append(attr)
        applied = False
        if truthy(request.data.get("apply_fee")) and fields:
            suggested = suggest_annual_fee(tenant)
            if suggested is None:
                return Response(
                    {"errors": {"message": "No default active pricing plan to price from."}},
                    status=400,
                )
            tenant.annual_fee = suggested
            tenant.fees_manually_set = False
            fields += ["annual_fee", "fees_manually_set"]
            applied = True
        if not fields:
            return Response({"errors": {"message": "Nothing to update."}}, status=400)
        tenant.save(update_fields=list(dict.fromkeys(fields)))

        synced = 0
        if truthy(request.data.get("sync_branches")):
            synced = tenant.branches.update(
                module_inventory=tenant.module_inventory,
                module_hr=tenant.module_hr,
                sales_role_enabled=tenant.sales_role_enabled,
            )
        log(
            request.user,
            "TENANT_MODULES_UPDATED",
            "tenant",
            tenant.pk,
            f"{tenant.name} · {sorted(set(fields))}"
            + (f" · branches synced={synced}" if synced else ""),
        )
        payload = module_flags(tenant)
        payload.update(
            {
                "annual_fee": money(tenant.annual_fee),
                "fee_applied": applied,
                "branches_synced": synced,
                "tenant": TenantSerializer(tenant).data,
            }
        )
        return Response({"data": payload})


class TenantReportingView(APIView):
    """One wholesaler's operational snapshot for the Reports tab.

    ?days=1..365 (default 90) windows movements, settlements and approvals;
    stock and staff figures are always current.
    """

    @require_platform_admin
    def get(self, request, pk):
        tenant = Tenant.objects.filter(pk=pk).first()
        if tenant is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        try:
            days = max(1, min(int(request.query_params.get("days") or 90), 365))
        except ValueError:
            days = 90
        since = timezone.now() - timedelta(days=days)
        lots = StockLot.objects.filter(tenant=tenant)
        stock_value = sum(
            (lot.remaining_quantity * lot.unit_cost for lot in lots), ZERO
        )
        settlements = SalesSettlement.objects.filter(
            tenant=tenant, created_at__gte=since
        )
        settled = settlements.filter(status="APPROVED")
        movements = StockMovement.objects.filter(
            tenant=tenant, at__gte=since
        ).values("type").annotate(count=Count("id"), quantity=Sum("quantity_delta"))
        from user.models import Profile

        roles = {
            row["role"]: row["c"]
            for row in Profile.objects.filter(tenant=tenant)
            .values("role")
            .annotate(c=Count("id"))
        }

        return Response(
            {
                "data": {
                    "window_days": days,
                    "inventory": {
                        "items": Item.objects.filter(tenant=tenant).count(),
                        "lots": lots.count(),
                        "stock_value": money(stock_value),
                        "out_of_stock": lots.filter(remaining_quantity=0).count(),
                        "registrations": status_counts(
                            RegistrationRequest.objects.filter(tenant=tenant)
                        ),
                        "stockouts": status_counts(
                            StockOutRequest.objects.filter(tenant=tenant)
                        ),
                        "movements": [
                            {
                                "type": row["type"],
                                "count": row["count"],
                                "quantity": str(row["quantity"] or 0),
                            }
                            for row in sorted(movements, key=lambda r: r["type"])
                        ],
                    },
                    "sales": {
                        "agents": SalesAgent.objects.filter(tenant=tenant).count(),
                        "agents_active": SalesAgent.objects.filter(
                            tenant=tenant, is_active=True
                        ).count(),
                        "settlements": settlements.count(),
                        "by_status": status_counts(settlements),
                        "sold_quantity": str(
                            settled.aggregate(v=Sum("sold_quantity"))["v"] or 0
                        ),
                        "sales_amount": money(settled.aggregate(v=Sum("sales_amount"))["v"]),
                        "expected_amount": money(
                            settled.aggregate(v=Sum("expected_amount"))["v"]
                        ),
                        "shortfall": money(
                            (settled.aggregate(v=Sum("expected_amount"))["v"] or ZERO)
                            - (settled.aggregate(v=Sum("sales_amount"))["v"] or ZERO)
                        ),
                        "penalised": settlements.filter(penalty=True).count(),
                    },
                    "assets": {
                        "count": Asset.objects.filter(tenant=tenant).count(),
                        "available": Asset.objects.filter(
                            tenant=tenant, status="AVAILABLE"
                        ).count(),
                        "assigned": Asset.objects.filter(
                            tenant=tenant, status="ASSIGNED"
                        ).count(),
                        "total_cost": money(
                            Asset.objects.filter(tenant=tenant).aggregate(
                                v=Sum(F("unit_cost") * F("quantity"))
                            )["v"]
                        ),
                    },
                    "people": {
                        "staff": sum(roles.values()),
                        "by_role": roles,
                        "logins_disabled": Profile.objects.filter(
                            tenant=tenant, login_disabled=True
                        ).count(),
                        "owners": Profile.objects.filter(
                            tenant=tenant, role="OWNER"
                        ).count(),
                    },
                    "payments": {
                        "subscriptions": status_counts(
                            SubscriptionPayment.objects.filter(tenant=tenant)
                        ),
                        "revenue": money(
                            SubscriptionPayment.objects.filter(
                                tenant=tenant, status="APPROVED"
                            ).aggregate(v=Sum("amount"))["v"]
                        ),
                        "setup": status_counts(
                            SetupPayment.objects.filter(tenant=tenant)
                        ),
                        "module_requests": status_counts(
                            ModuleRequest.objects.filter(tenant=tenant)
                        ),
                    },
                    "approvals": {
                        "decisions": Approval.objects.filter(
                            tenant=tenant, decided_at__gte=since
                        ).count(),
                        "rejected": Approval.objects.filter(
                            tenant=tenant, decided_at__gte=since, decision="REJECTED"
                        ).count(),
                    },
                    "chat": {
                        "threads": ChatThread.objects.filter(tenant=tenant).count(),
                        "messages": ChatMessage.objects.filter(
                            thread__tenant=tenant
                        ).count(),
                    },
                }
            }
        )


class TenantWithoutOwnerView(APIView):
    """Companies with no OWNER login — the console's onboarding gap list."""

    @require_platform_admin
    def get(self, request):
        from user.models import Profile

        owner_tenant_ids = set(
            Profile.objects.filter(role="OWNER").values_list("tenant_id", flat=True)
        )
        tenants = Tenant.objects.filter(parent__isnull=True).exclude(
            pk__in=owner_tenant_ids
        )
        term = (request.query_params.get("search") or "").strip()
        if term:
            tenants = tenants.filter(
                Q(name__icontains=term) | Q(tin__icontains=term) | Q(phone__icontains=term)
            )
        if (request.query_params.get("branchless") or "").lower() in ("1", "true", "yes"):
            tenants = tenants.annotate(c=Count("branches")).filter(c=0)
        rows = []
        for tenant in tenants.annotate(staff_count=Count("staff", distinct=True)).order_by(
            "-created_at"
        ):
            roles = sorted(
                {
                    profile.role
                    for profile in Profile.objects.filter(tenant=tenant).only("role")
                }
            )
            rows.append(
                {
                    "id": tenant.id,
                    "name": tenant.name,
                    "tin": tenant.tin,
                    "phone": tenant.phone,
                    "created_at": tenant.created_at,
                    "staff_count": tenant.staff_count,
                    "roles": roles,
                    "branch_count": tenant.branches.count(),
                    "sales_agent": tenant.sales_agent.name if tenant.sales_agent else "",
                    "subscription_state": tenant.subscription_state,
                    "billing_state": tenant.billing_state,
                    "annual_fee": money(tenant.annual_fee),
                    "module_inventory": tenant.module_inventory,
                    "module_hr": tenant.module_hr,
                }
            )
        return Response({"data": rows, "meta": {"total": len(rows)}})


class OwnerCandidateView(APIView):
    """GET /api/tenants/<pk>/owner/candidates/?search= — accounts that can be
    promoted to this wholesaler's owner: unassigned logins and this tenant's
    own staff. Platform admins and other tenants' owners are never offered."""

    @require_platform_admin
    def get(self, request, pk):
        from django.contrib.auth import get_user_model

        from user.models import Profile

        tenant = Tenant.objects.filter(pk=pk).first()
        if tenant is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        User = get_user_model()
        term = (request.query_params.get("search") or "").strip()
        owner_user_ids = set(
            Profile.objects.filter(role="OWNER").values_list("user_id", flat=True)
        )
        profiles = (
            Profile.objects.select_related("user", "tenant")
            .exclude(user_id__in=owner_user_ids)
            .filter(Q(tenant__isnull=True) | Q(tenant=tenant))
        )
        if term:
            profiles = profiles.filter(
                Q(user__username__icontains=term)
                | Q(user__first_name__icontains=term)
                | Q(user__last_name__icontains=term)
                | Q(user__email__icontains=term)
            )
        rows = [
            {
                "id": profile.user_id,
                "username": profile.user.username,
                "name": profile.user.get_full_name() or profile.user.username,
                "email": profile.user.email or "",
                "role": profile.role,
                "tenant": profile.tenant.name if profile.tenant else "",
                "tenant_id": profile.tenant_id,
                "last_login": profile.user.last_login,
            }
            for profile in profiles.order_by("user__username")[:100]
        ]
        # An account with no Profile row at all is still linkable.
        if term:
            loose = (
                User.objects.filter(Q(profile__isnull=True))
                .exclude(is_superuser=True, is_staff=True)
                .filter(
                    Q(username__icontains=term) | Q(email__icontains=term)
                )[:20]
            )
            rows.extend(
                {
                    "id": user.id,
                    "username": user.username,
                    "name": user.get_full_name() or user.username,
                    "email": user.email or "",
                    "role": "",
                    "tenant": "",
                    "tenant_id": None,
                    "last_login": user.last_login,
                }
                for user in loose
            )
        return Response({"data": rows})


class TenantOwnerLinkView(APIView):
    """POST /api/tenants/<pk>/owner/link/ {user_id, replace?}

    Attach an existing login to this wholesaler as its OWNER instead of
    creating a second one.
    """

    @require_platform_admin
    def post(self, request, pk):
        from django.contrib.auth import get_user_model

        from user.models import Profile

        tenant = Tenant.objects.filter(pk=pk).first()
        if tenant is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        User = get_user_model()
        user = User.objects.filter(pk=request.data.get("user_id")).first()
        if user is None:
            return Response({"errors": {"message": "Pick an account."}}, status=400)
        if user.is_superuser:
            return Response(
                {"errors": {"message": "A platform admin cannot become a wholesaler owner."}},
                status=400,
            )
        profile = Profile.objects.filter(user=user).first()
        if profile and profile.role == "OWNER" and profile.tenant_id != tenant.pk:
            return Response(
                {
                    "errors": {
                        "message": f"@{user.username} already owns {profile.tenant.name}."
                    }
                },
                status=400,
            )
        current = Profile.objects.filter(tenant=tenant, role="OWNER").select_related("user")
        if current.exists() and not truthy(request.data.get("replace")):
            return Response(
                {
                    "errors": {
                        "message": f"@{current.first().user.username} is already the owner here. Send replace to move it."
                    }
                },
                status=400,
            )
        if current.exists():
            for old in current:
                old.role = "MANAGER"
                old.save(update_fields=["role"])
        if profile is None:
            Profile.objects.create(user=user, tenant=tenant, role="OWNER")
        else:
            profile.tenant = tenant
            profile.role = "OWNER"
            profile.save(update_fields=["tenant", "role"])
        log(
            request.user,
            "OWNER_LINKED",
            "user",
            user.pk,
            f"@{user.username} → {tenant.name}",
        )
        return Response(
            {
                "data": {
                    "id": user.id,
                    "username": user.username,
                    "name": user.get_full_name() or user.username,
                    "email": user.email or "",
                    "last_login": user.last_login,
                    "must_change_password": getattr(
                        getattr(user, "profile", None), "must_change_password", False
                    ),
                    "tenant": tenant.name,
                }
            }
        )


class StaffLoginToggleView(APIView):
    """POST /api/tenants/staff/<user_id>/login/ {disabled, reason?}

    One staff login on or off — the tenant keeps working through its other
    accounts. Disabling needs a reason; the VoloSync app refuses the login
    while the flag is set.
    """

    @require_platform_admin
    def post(self, request, pk):
        from user.models import Profile

        profile = (
            Profile.objects.select_related("user", "tenant", "login_disabled_by")
            .filter(user_id=pk)
            .first()
        )
        if profile is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        disabled = request.data.get("disabled")
        disabled = truthy(disabled) if not isinstance(disabled, bool) else disabled
        reason = (request.data.get("reason") or "").strip()
        if disabled and len(reason) < 4:
            return Response(
                {"errors": {"message": "A reason of at least 4 characters is required."}},
                status=400,
            )
        profile.login_disabled = disabled
        profile.login_disabled_reason = reason if disabled else ""
        profile.login_disabled_at = timezone.now() if disabled else None
        profile.login_disabled_by = request.user if disabled else None
        profile.save(
            update_fields=[
                "login_disabled",
                "login_disabled_reason",
                "login_disabled_at",
                "login_disabled_by",
            ]
        )
        log(
            request.user,
            "STAFF_LOGIN_" + ("DISABLED" if disabled else "ENABLED"),
            "user",
            profile.user_id,
            f"@{profile.user.username}" + (f" · {reason}" if reason else ""),
            reason=reason,
        )
        return Response({"data": staff_row(profile)})
