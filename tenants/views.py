import secrets
import string
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from inventory.models import StockLot
from user.platform import require_platform_admin

from .models import Feedback, ModuleRequest, SetupPayment, SubscriptionPayment, Tenant
from .pricing import suggest_annual_fee
from .serializers import TenantSerializer
from .services import (
    approve_subscription,
    create_subscription_submission,
    payment_row,
)

User = get_user_model()


class TenantMeView(APIView):
    @require_platform_admin
    def get(self, request):
        profile = getattr(request.user, "profile", None)
        if not profile or not profile.tenant:
            return Response({"data": None})
        return Response({"data": TenantSerializer(profile.tenant).data})


class TenantListView(APIView):
    """Platform list: every wholesaler plus its branches."""

    @require_platform_admin
    def post(self, request):
        """Signup: a new wholesaler tenant plus its manager account.

        The credential collected here is the tenant's MANAGER (it signs in on
        VoloSync). The OWNER login is added later from Tenant detail -> Owner
        tab and signs in on the VoloSync Owner app.
        """
        name = (request.data.get("name") or "").strip()
        tin = (request.data.get("tin") or "").strip()
        username = (request.data.get("username") or "").strip()
        password = request.data.get("password") or ""
        if not name or not tin:
            return Response(
                {"errors": {"message": "Company name and TIN are required."}}, status=400
            )
        if Tenant.objects.filter(tin=tin).exists():
            return Response({"errors": {"message": "That TIN is taken."}}, status=400)
        if not username or len(password) < 6:
            return Response(
                {"errors": {"message": "A manager username and a 6+ character password are required."}},
                status=400,
            )
        if User.objects.filter(username=username).exists():
            return Response({"errors": {"message": "That username is taken."}}, status=400)

        logo_url = (request.data.get("logo_url") or "").strip()
        if logo_url and not logo_url.startswith(("https://", "http://")):
            return Response(
                {"errors": {"message": "The logo must be an uploaded image URL."}},
                status=400,
            )

        sales_agent = None
        agent_id = request.data.get("sales_agent")
        if agent_id not in (None, "", "null"):
            from .models import PlatformSalesAgent as _Agent

            sales_agent = _Agent.objects.filter(pk=agent_id).first()
            if not sales_agent:
                return Response(
                    {"errors": {"message": "That sales agent does not exist."}}, status=400
                )

        from user.models import Profile

        tenant = Tenant.objects.create(
            name=name,
            tin=tin,
            phone=request.data.get("phone", ""),
            address=request.data.get("address", ""),
            logo_url=logo_url,
            vat_rate=request.data.get("vat_rate") or "15.00",
            module_inventory=bool(request.data.get("module_inventory", True)),
            module_hr=bool(request.data.get("module_hr", True)),
            sales_role_enabled=bool(request.data.get("sales_role_enabled", False)),
            sales_agent=sales_agent,
            is_main=True,
            subscription_expires_on=timezone.localdate() + timedelta(days=365),
        )
        manager = User.objects.create_user(
            username=username,
            password=password,
            email=(request.data.get("email") or "").strip(),
            first_name=request.data.get("manager_first_name", ""),
            last_name=request.data.get("manager_last_name", ""),
        )
        # MANAGER: this credential opens the VoloSync manager portal. The OWNER
        # login is created separately from the tenant detail Owner tab.
        Profile.objects.create(user=manager, tenant=tenant, role="MANAGER")
        from .platform_views import log

        log(request.user, "TENANT_CREATED", "tenant", tenant.pk, tenant.name)
        log(request.user, "MANAGER_CREATED", "user", manager.pk, tenant.name)
        return Response(
            {"data": TenantSerializer(tenant).data, "manager": manager.username}, status=201
        )

    @require_platform_admin
    def get(self, request):
        tenants = Tenant.objects.select_related("sales_agent").annotate(
            staff_count=Count("staff", distinct=True),
            branch_count=Count("branches", distinct=True),
            lot_count=Count("lots", distinct=True),
        ).prefetch_related("branches")
        agent_filter = request.query_params.get("sales_agent")
        if agent_filter:
            tenants = tenants.filter(sales_agent_id=agent_filter)
        payload = []
        for tenant in tenants:
            payload.append(
                {
                    **TenantSerializer(tenant).data,
                    "staff_count": tenant.staff_count,
                    "branch_count": tenant.branch_count,
                    "lot_count": tenant.lot_count,
                    "is_branch": tenant.parent_id is not None,
                    "stock_value": str(
                        sum(
                            (
                                lot.remaining_quantity * lot.unit_cost
                                for lot in StockLot.objects.filter(tenant=tenant)
                            ),
                            0,
                        )
                    ),
                }
            )
        return Response({"data": payload})


class TenantDetailView(APIView):
    """One tenant: full profile for the console's 7-tab detail page."""

    @require_platform_admin
    def get(self, request, pk):
        tenant = Tenant.objects.filter(pk=pk).first()
        if not tenant:
            return Response({"errors": {"message": "Not found."}}, status=404)
        lots = StockLot.objects.filter(tenant=tenant)
        stock_value = sum(
            (lot.remaining_quantity * lot.unit_cost for lot in lots),
            Decimal("0.00"),
        )
        suggested = suggest_annual_fee(tenant)
        payload = {
            **TenantSerializer(tenant).data,
            "is_branch": tenant.parent_id is not None,
            "parent_name": tenant.parent.name if tenant.parent else None,
            # Yearly billing: the renewal falls due on the expiry date.
            # null = never paid (subscription_state UNPAID).
            "next_renewal_date": tenant.subscription_expires_on,
            "suggested_annual_fee": str(suggested) if suggested is not None else None,
            "counts": {
                "staff": tenant.staff.count(),
                "branches": tenant.branches.count(),
                "lots": lots.count(),
                "payments": tenant.subscription_payments.count(),
                "setup_payments": tenant.setup_payments.count(),
                "module_requests": tenant.module_requests.count(),
            },
            "stock_value": str(stock_value),
        }
        return Response({"data": payload})

    @require_platform_admin
    def patch(self, request, pk):
        """Whitelisted field updates + optional fee recalculation.

        Every PATCH returns `suggested_annual_fee` (from the default active
        pricing plan, computed AFTER the module toggles in this payload are
        applied; null when no default active plan exists). The suggestion is
        STORED on tenant.annual_fee only when the client sends
        `apply_fee: true` — the UI shows the recalculation first, then
        optionally applies it. `apply_fee` itself is never stored.
        """
        tenant = Tenant.objects.filter(pk=pk).first()
        if not tenant:
            return Response({"errors": {"message": "Not found."}}, status=404)
        allowed = [
            "name",
            "phone",
            "address",
            "logo_url",
            "vat_rate",
            "module_inventory",
            "module_hr",
            "sales_role_enabled",
            "subscription_expires_on",
            "annual_fee",
            "subscription_note",
            "sales_agent",
        ]
        if "annual_fee" in request.data:
            try:
                tenant.annual_fee = Decimal(str(request.data["annual_fee"] or "0"))
            except InvalidOperation:
                return Response(
                    {"errors": {"message": "annual_fee must be a number."}}, status=400
                )
        for field in allowed:
            if field in request.data and field != "annual_fee":
                setattr(tenant, field, request.data[field])
        tenant.save()

        suggested = suggest_annual_fee(tenant)
        apply_fee = str(request.data.get("apply_fee", "")).lower() in ("1", "true", "yes")
        fee_applied = False
        if apply_fee and suggested is not None:
            tenant.annual_fee = suggested
            tenant.save(update_fields=["annual_fee"])
            fee_applied = True

        from .platform_views import log

        detail = str(sorted(request.data))
        if fee_applied:
            detail += f" · annual_fee={tenant.annual_fee}"
        log(request.user, "TENANT_UPDATED", "tenant", tenant.pk, detail)
        return Response(
            {
                "data": TenantSerializer(tenant).data,
                "suggested_annual_fee": str(suggested) if suggested is not None else None,
                "fee_applied": fee_applied,
            }
        )


class TenantPaymentsView(APIView):
    """One tenant's yearly subscription payment history, newest first."""

    @require_platform_admin
    def get(self, request, pk):
        tenant = Tenant.objects.filter(pk=pk).first()
        if not tenant:
            return Response({"errors": {"message": "Not found."}}, status=404)
        rows = (
            SubscriptionPayment.objects.filter(tenant=tenant)
            .select_related("tenant", "submitted_by", "decided_by", "recorded_by")
            .order_by("-paid_on", "-id")
        )
        return Response({"data": [payment_row(row) for row in rows]})


class TenantStaffView(APIView):
    """Users of one tenant with role, last login and credential state."""

    @require_platform_admin
    def get(self, request, pk):
        tenant = Tenant.objects.filter(pk=pk).first()
        if not tenant:
            return Response({"errors": {"message": "Not found."}}, status=404)
        from user.models import Profile

        profiles = (
            Profile.objects.filter(tenant=tenant)
            .select_related("user")
            .order_by("role", "user__username")
        )
        return Response(
            {
                "data": [
                    {
                        "id": profile.user_id,
                        "profile_id": profile.id,
                        "username": profile.user.username,
                        "name": profile.user.get_full_name() or profile.user.username,
                        "role": profile.role,
                        "phone": profile.phone,
                        "last_login": profile.user.last_login,
                        "must_change_password": profile.must_change_password,
                        "is_active": profile.user.is_active,
                    }
                    for profile in profiles
                ]
            }
        )


class StaffResetPasswordView(APIView):
    """Reset a staff member's password.

    POST {"password": "..."}  → sets that password (6+ chars).
    POST {}                   → generates a random 12-char temp password.
    The password is returned ONCE in the response (`password`), and the
    action is audit-logged. It is never retrievable again.
    """

    @require_platform_admin
    def post(self, request, pk):
        target = User.objects.filter(pk=pk).first()
        if not target:
            return Response({"errors": {"message": "Not found."}}, status=404)

        password = request.data.get("password") or ""
        generated = not password
        if generated:
            alphabet = string.ascii_letters + string.digits
            password = "".join(secrets.choice(alphabet) for _ in range(12))
        elif len(password) < 6:
            return Response(
                {"errors": {"message": "Use at least 6 characters."}}, status=400
            )

        target.set_password(password)
        target.save(update_fields=["password"])

        from .platform_views import log

        log(
            request.user,
            "PASSWORD_RESET",
            "user",
            target.pk,
            target.username + (" (generated)" if generated else " (manual)"),
        )
        return Response(
            {
                "data": {
                    "id": target.id,
                    "username": target.username,
                    "password": password,
                    "generated": generated,
                    "must_change_password": False,
                }
            }
        )


class TenantOwnerView(APIView):
    """The tenant's OWNER account (Tenant detail → Owner tab).

    GET   → {id, username, email, name, last_login, must_change_password}.
    POST  {"username", "password", "email"?} → creates the owner login.
    PATCH {"username"?, "email"?} → updates the owner's login/contact email.
    Password resets go through POST /api/tenants/staff/<id>/reset-password/.
    """

    def _owner(self, pk):
        from user.models import Profile

        tenant = Tenant.objects.filter(pk=pk).first()
        if not tenant:
            return None, None
        profile = (
            Profile.objects.filter(tenant=tenant, role="OWNER")
            .select_related("user")
            .order_by("id")
            .first()
        )
        return tenant, (profile.user if profile else None)

    @require_platform_admin
    def get(self, request, pk):
        tenant, owner = self._owner(pk)
        if not tenant:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if not owner:
            return Response({"data": None})
        return Response({"data": _owner_payload(owner)})

    @require_platform_admin
    def post(self, request, pk):
        from user.models import Profile

        tenant, owner = self._owner(pk)
        if not tenant:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if owner:
            return Response(
                {"errors": {"message": "This tenant already has an owner account."}},
                status=400,
            )
        username = (request.data.get("username") or "").strip()
        password = request.data.get("password") or ""
        email = (request.data.get("email") or "").strip()
        if not username:
            return Response(
                {"errors": {"message": "Enter an owner username."}}, status=400
            )
        if len(password) < 6:
            return Response(
                {"errors": {"message": "Use a password of at least 6 characters."}},
                status=400,
            )
        if User.objects.filter(username__iexact=username).exists():
            return Response(
                {"errors": {"message": "That username is already taken."}}, status=400
            )
        user = User.objects.create_user(username=username, password=password, email=email)
        Profile.objects.create(user=user, tenant=tenant, role="OWNER")
        from .platform_views import log

        log(request.user, "OWNER_CREATED", "user", user.pk, username)
        return Response({"data": _owner_payload(user)}, status=201)

    @require_platform_admin
    def patch(self, request, pk):
        tenant, owner = self._owner(pk)
        if not tenant:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if not owner:
            return Response(
                {"errors": {"message": "This tenant has no owner account."}}, status=404
            )
        updated = []
        if "username" in request.data:
            username = (request.data.get("username") or "").strip()
            if not username:
                return Response(
                    {"errors": {"message": "Enter an owner username."}}, status=400
                )
            if (
                User.objects.filter(username__iexact=username)
                .exclude(pk=owner.pk)
                .exists()
            ):
                return Response(
                    {"errors": {"message": "That username is already taken."}},
                    status=400,
                )
            owner.username = username
            updated.append("username")
        if "email" in request.data:
            owner.email = (request.data.get("email") or "").strip()
            updated.append("email")
        if not updated:
            return Response(
                {"errors": {"message": "Nothing to update."}}, status=400
            )
        owner.save(update_fields=updated)
        from .platform_views import log

        log(
            request.user,
            "OWNER_UPDATED",
            "user",
            owner.pk,
            f"@{owner.username} email={owner.email}",
        )
        return Response({"data": _owner_payload(owner)})


def _owner_payload(owner):
    return {
        "id": owner.id,
        "username": owner.username,
        "email": owner.email or "",
        "name": owner.get_full_name() or owner.username,
        "last_login": owner.last_login,
        "must_change_password": getattr(
            getattr(owner, "profile", None), "must_change_password", False
        ),
    }


class AdminDashboardView(APIView):
    """Platform overview: tenants, subscription health and yearly revenue."""

    @require_platform_admin
    def get(self, request):
        today = timezone.localdate()
        tenants = Tenant.objects.all()
        companies = tenants.filter(parent__isnull=True)
        soon = today + timedelta(days=30)
        payments = SubscriptionPayment.objects.all()
        year_start = today.replace(month=1, day=1)
        revenue = payments.filter(paid_on__gte=year_start).aggregate(
            total=Sum("amount")
        )["total"] or Decimal("0.00")
        stock_value = sum(
            (
                lot.remaining_quantity * lot.unit_cost
                for lot in StockLot.objects.all()
            ),
            Decimal("0.00"),
        )
        return Response(
            {
                "data": {
                    "companies": companies.count(),
                    "branches": tenants.filter(parent__isnull=False).count(),
                    "active": companies.filter(
                        subscription_expires_on__gte=today
                    ).count(),
                    "due_soon": companies.filter(
                        subscription_expires_on__gte=today,
                        subscription_expires_on__lte=soon,
                    ).count(),
                    "expired": companies.filter(subscription_expires_on__lt=today).count()
                    + companies.filter(subscription_expires_on__isnull=True).count(),
                    "year_revenue": str(revenue),
                    "payments": payments.count(),
                    "stock_value": str(stock_value),
                    "staff": get_user_model().objects.count(),
                    "pending_subscription_payments": payments.filter(
                        status="PENDING"
                    ).count(),
                    "pending_setup_payments": SetupPayment.objects.filter(
                        status="PENDING"
                    ).count(),
                    "pending_module_requests": ModuleRequest.objects.filter(
                        status="PENDING"
                    ).count(),
                    "unread_feedback": Feedback.objects.filter(unread=True).count(),
                }
            }
        )


class SubscriptionListCreateView(APIView):
    """Yearly subscription ledger (queue-aware).

    GET  — global list, newest first; rows carry channel/status/decision
           fields so the console can render the verification queue.

    POST — legacy direct record:
             · default              → PENDING row, tenant NOT extended
                                       (same as POST /api/payments/subscription/)
             · ?approved=true       → record AND extend immediately
                                       (status APPROVED, decided_by = caller)
    """

    @require_platform_admin
    def get(self, request):
        rows = (
            SubscriptionPayment.objects.select_related(
                "tenant", "recorded_by", "submitted_by", "decided_by"
            )[:200]
        )
        return Response({"data": [payment_row(row) for row in rows]})

    @require_platform_admin
    def post(self, request):
        tenant = Tenant.objects.filter(pk=request.data.get("tenant")).first()
        if tenant is None:
            return Response({"errors": {"message": "Pick a tenant."}}, status=400)
        transaction_id = (
            request.data.get("transaction_id") or request.data.get("transaction_ref") or ""
        ).strip()
        if not transaction_id:
            return Response(
                {"errors": {"message": "The subscription transaction id is required."}},
                status=400,
            )
        years = int(request.data.get("years") or 1)
        years = max(1, min(years, 5))
        amount = Decimal(str(request.data.get("amount") or "0"))
        note = request.data.get("note", "")
        channel = (request.data.get("channel") or "OTHER").upper()
        if channel not in {"TELEBIRR", "BANK", "CASH", "OTHER"}:
            channel = "OTHER"

        approved_now = str(
            request.query_params.get("approved", request.data.get("approved", ""))
        ).lower() in ("1", "true", "yes")

        payment = create_subscription_submission(
            request.user, tenant, channel, transaction_id, amount, years, note
        )
        covers_until = payment.covers_until
        if approved_now:
            covers_until = approve_subscription(payment, request.user, note=note)
            from .platform_views import log

            log(
                request.user,
                "SUBSCRIPTION_RECORDED",
                "tenant",
                tenant.pk,
                transaction_id,
            )
        else:
            from .platform_views import log

            log(
                request.user,
                "SUBSCRIPTION_SUBMITTED",
                "subscription_payment",
                payment.pk,
                transaction_id,
            )
        return Response(
            {
                "data": {
                    "payment": payment.id,
                    "status": payment.status,
                    "tenant": TenantSerializer(tenant).data,
                    "covers_until": covers_until,
                }
            },
            status=201,
        )


class StaffListView(APIView):
    """Every platform account, grouped by tenant and role.

    `id` is the USER id (the reset-password endpoint targets it);
    `profile_id` is kept for reference.
    """

    @require_platform_admin
    def get(self, request):
        from user.models import Profile

        profiles = Profile.objects.select_related("user", "tenant").order_by(
            "tenant__name", "role"
        )
        return Response(
            {
                "data": [
                    {
                        "id": profile.user_id,
                        "profile_id": profile.id,
                        "username": profile.user.username,
                        "name": profile.user.get_full_name() or profile.user.username,
                        "role": profile.role,
                        "tenant": profile.tenant.name if profile.tenant else "",
                        "tenant_id": profile.tenant_id,
                        "phone": profile.phone,
                        "last_login": profile.user.last_login,
                        "must_change_password": profile.must_change_password,
                        "is_active": profile.user.is_active,
                    }
                    for profile in profiles
                ]
            }
        )


class BranchCreateView(APIView):
    """Child tenants of one company: GET the list, POST to create one."""

    @require_platform_admin
    def get(self, request, pk):
        parent = Tenant.objects.filter(pk=pk).first()
        if not parent:
            return Response({"errors": {"message": "Not found."}}, status=404)
        branches = (
            Tenant.objects.filter(parent=parent)
            .annotate(staff_count=Count("staff", distinct=True))
            .order_by("-is_main", "name")
        )
        return Response(
            {
                "data": [
                    {
                        **TenantSerializer(branch).data,
                        "is_branch": True,
                        "staff_count": branch.staff_count,
                    }
                    for branch in branches
                ]
            }
        )

    @require_platform_admin
    def post(self, request, pk):
        parent = Tenant.objects.filter(pk=pk).first()
        if not parent:
            return Response({"errors": {"message": "Not found."}}, status=404)

        name = (request.data.get("name") or "").strip()
        tin = (request.data.get("tin") or "").strip()
        if not name or not tin:
            return Response(
                {"errors": {"message": "Branch name and TIN are required."}}, status=400
            )
        if tin == parent.tin:
            return Response(
                {"errors": {"message": "A branch must have its own TIN."}}, status=400
            )
        if Tenant.objects.filter(tin=tin).exists():
            return Response(
                {"errors": {"message": "That TIN already exists."}}, status=400
            )

        branch = Tenant.objects.create(
            name=name,
            tin=tin,
            phone=request.data.get("phone", ""),
            address=request.data.get("address", ""),
            vat_rate=parent.vat_rate,
            module_inventory=parent.module_inventory,
            module_hr=parent.module_hr,
            sales_role_enabled=parent.sales_role_enabled,
            parent=parent,
        )

        username = (request.data.get("username") or "").strip()
        password = request.data.get("password") or ""
        if username and password:
            if User.objects.filter(username=username).exists():
                return Response(
                    {"errors": {"message": "That username is taken."}}, status=400
                )
            user = User.objects.create_user(
                username=username,
                password=password,
                first_name=request.data.get("manager_first_name", "Branch"),
                last_name=request.data.get("manager_last_name", "Manager"),
            )
            from user.models import Profile

            Profile.objects.create(user=user, tenant=branch, role="MANAGER")

        from .platform_views import log

        log(request.user, "BRANCH_CREATED", "tenant", branch.pk, branch.name)
        return Response({"data": TenantSerializer(branch).data}, status=201)
