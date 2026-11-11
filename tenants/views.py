import secrets
import string
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.db.models import Count, F, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from inventory.models import StockLot
from user.platform import require_platform_admin

from .models import (
    ChatMessage,
    ChatThread,
    Feedback,
    PlatformSalesAgent,
    SetupPayment,
    SubscriptionPayment,
    Tenant,
)
from .platform_views import log, truthy
from .pricing import default_plan, suggest_fees
from .serializers import TenantSerializer
from .services import (
    approve_subscription,
    create_subscription_submission,
    fees_for_modules,
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
            sales_agent = PlatformSalesAgent.objects.filter(pk=agent_id).first()
            if not sales_agent:
                return Response(
                    {"errors": {"message": "That sales agent does not exist."}}, status=400
                )

        from user.models import Profile

        plan = default_plan()
        tenant = Tenant.objects.create(
            name=name,
            tin=tin,
            phone=request.data.get("phone", ""),
            address=request.data.get("address", ""),
            logo_url=logo_url,
            vat_rate=request.data.get("vat_rate") or "15.00",
            module_inventory=truthy(request.data.get("module_inventory", True)),
            module_hr=truthy(request.data.get("module_hr", True)),
            sales_role_enabled=truthy(request.data.get("sales_role_enabled", False)),
            sales_agent=sales_agent,
            is_main=True,
            subscription_expires_on=timezone.localdate() + timedelta(days=365),
            # New wholesalers start with the catalog's one-time setup fee; the
            # console can override it per tenant on the billing tab.
            setup_fee=plan.setup_fee if plan else None,
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

        log(request.user, "TENANT_CREATED", "tenant", tenant.pk, tenant.name)
        log(request.user, "MANAGER_CREATED", "user", manager.pk, tenant.name)
        return Response(
            {"data": TenantSerializer(tenant).data, "manager": manager.username}, status=201
        )

    @require_platform_admin
    def get(self, request):
        """Server-side console filters.

        ?search= name/TIN/phone · ?state= ACTIVE|DUE_SOON|EXPIRED|UNPAID
        ?access= ACTIVE|SUSPENDED|BANNED|DELETED|INACTIVE · ?branch= true|false
        ?sales_agent= <id>|none · ?module= INVENTORY|HR|SALES_ROLE
        ?limit/offset — deleted wholesalers stay out unless asked for.
        """
        params = request.query_params
        tenants = Tenant.objects.select_related("sales_agent", "parent").annotate(
            staff_count=Count("staff", distinct=True),
            branch_count=Count("branches", distinct=True),
            lot_count=Count("lots", distinct=True),
        )
        term = (params.get("search") or "").strip()
        if term:
            tenants = tenants.filter(
                Q(name__icontains=term)
                | Q(tin__icontains=term)
                | Q(phone__icontains=term)
                | Q(parent__name__icontains=term)
            )
        access = (params.get("access") or "").upper()
        if access == "INACTIVE":
            # The restricted watchlist: everything that cannot open its app.
            tenants = tenants.filter(
                account_status__in=("SUSPENDED", "BANNED", "DELETED")
            )
        elif access in dict(Tenant.ACCESS_STATES):
            tenants = tenants.filter(account_status=access)
        elif access != "DELETED":
            tenants = tenants.exclude(account_status="DELETED")

        state = (params.get("state") or "").upper()
        today = timezone.localdate()
        if state == "UNPAID":
            tenants = tenants.filter(subscription_expires_on__isnull=True)
        elif state == "EXPIRED":
            tenants = tenants.filter(subscription_expires_on__lt=today)
        elif state == "DUE_SOON":
            tenants = tenants.filter(
                subscription_expires_on__gte=today,
                subscription_expires_on__lte=today + timedelta(days=30),
            )
        elif state == "ACTIVE":
            tenants = tenants.filter(subscription_expires_on__gt=today + timedelta(days=30))

        billing_state = (params.get("billing_state") or "").upper()
        branch = (params.get("branch") or "").strip().lower()
        if branch in ("1", "true", "yes"):
            tenants = tenants.filter(parent__isnull=False)
        elif branch in ("0", "false", "no"):
            tenants = tenants.filter(parent__isnull=True)

        agent = (params.get("sales_agent") or "").strip()
        if agent == "none":
            tenants = tenants.filter(sales_agent__isnull=True)
        elif agent:
            tenants = tenants.filter(sales_agent_id=agent)

        module = (params.get("module") or "").upper()
        if module == "INVENTORY":
            tenants = tenants.filter(module_inventory=True)
        elif module == "HR":
            tenants = tenants.filter(module_hr=True)
        elif module == "SALES_ROLE":
            tenants = tenants.filter(sales_role_enabled=True)
        elif module == "NONE":
            tenants = tenants.filter(module_inventory=False, module_hr=False)

        stock_values = {
            row["tenant_id"]: row["value"]
            for row in StockLot.objects.values("tenant_id").annotate(
                value=Sum(F("remaining_quantity") * F("unit_cost"))
            )
        }
        # `billing_state` is derived from illustration/hold/trial flags, so it
        # narrows in python once every SQL filter has run.
        rows = list(tenants)
        if billing_state:
            rows = [tenant for tenant in rows if tenant.billing_state == billing_state]

        try:
            limit = max(1, min(int(params.get("limit") or 500), 1000))
            offset = max(0, int(params.get("offset") or 0))
        except ValueError:
            limit, offset = 500, 0

        total = len(rows)
        page = rows[offset : offset + limit]

        # Three per-page joins the list columns need: who can sign in, and
        # whether that wholesaler is waiting on an unread reply.
        from user.models import Profile

        page_ids = [tenant.pk for tenant in page]
        lookup_ids = set(page_ids) | {
            tenant.parent_id for tenant in page if tenant.parent_id
        }
        logins: dict[int, dict[str, str]] = {}
        for profile in (
            Profile.objects.filter(
                tenant_id__in=lookup_ids, role__in=("OWNER", "MANAGER")
            )
            .select_related("user")
        ):
            logins.setdefault(profile.tenant_id, {})[profile.role] = profile.user.username
        unread = {
            row["thread__tenant_id"]: row["c"]
            for row in ChatMessage.objects.filter(
                thread__tenant_id__in=page_ids, side="TENANT", read_by_apex=False
            ).values("thread__tenant_id").annotate(c=Count("id"))
        }

        def owner_of(tenant):
            # A branch is signed into by its company's owner login, so the list
            # shows the parent's owner instead of an empty cell.
            own = logins.get(tenant.pk, {}).get("OWNER")
            return own or (
                logins.get(tenant.parent_id, {}).get("OWNER") if tenant.parent_id else None
            )

        # Setup review standing — a branch inherits its company's approval.
        approved_setup = set(
            SetupPayment.objects.filter(
                tenant_id__in=lookup_ids, status="APPROVED"
            ).values_list("tenant_id", flat=True)
        )

        def setup_ok(tenant):
            if tenant.pk in approved_setup:
                return True
            return bool(tenant.parent_id) and tenant.parent_id in approved_setup

        payload = [
            {
                **TenantSerializer(tenant).data,
                "staff_count": tenant.staff_count,
                "branch_count": tenant.branch_count,
                "lot_count": tenant.lot_count,
                "is_branch": tenant.parent_id is not None,
                "is_main": tenant.is_main,
                "parent_tin": tenant.parent.tin if tenant.parent else None,
                "parent_name": tenant.parent.name if tenant.parent else None,
                "stock_value": str(stock_values.get(tenant.pk) or Decimal("0.00")),
                "account_status": tenant.account_status,
                "billing_state": tenant.billing_state,
                "billing_hold": tenant.billing_hold,
                "setup_approved": setup_ok(tenant),
                "days_to_renewal": tenant.days_to_renewal,
                "owner_username": owner_of(tenant),
                "manager_username": logins.get(tenant.pk, {}).get("MANAGER"),
                "unread_chat": unread.get(tenant.pk, 0),
            }
            for tenant in page
        ]
        return Response({"data": payload, "meta": {"total": total, "limit": limit, "offset": offset}})


class TenantDetailView(APIView):
    """One tenant: full profile for the console's 7-tab detail page."""

    @require_platform_admin
    def get(self, request, pk):
        """The whole detail page in one payload (the console's tab shell)."""
        tenant = Tenant.objects.filter(pk=pk).first()
        if not tenant:
            return Response({"errors": {"message": "Not found."}}, status=404)
        from .console_views import access_payload, billing_payload, module_flags
        from user.models import Profile

        lots = StockLot.objects.filter(tenant=tenant)
        stock_value = sum(
            (lot.remaining_quantity * lot.unit_cost for lot in lots),
            Decimal("0.00"),
        )
        roles = {
            row["role"]: row["c"]
            for row in Profile.objects.filter(tenant=tenant)
            .values("role")
            .annotate(c=Count("id"))
        }
        thread = ChatThread.objects.filter(tenant=tenant).first()
        # Yearly billing ledger standing: how many years are paid for, and
        # whether the one-time setup fee cleared review (a branch inherits the
        # company's approval, because the company is what signed up).
        paid_years = (
            tenant.subscription_payments.filter(status="APPROVED").aggregate(
                years=Sum("years")
            )["years"]
            or 0
        )
        setup_holder = tenant.parent or tenant
        root = tenant.parent or tenant
        family = [root, *root.branches.all()]
        family.sort(key=lambda member: (not member.is_main, member.name.lower()))
        payload = {
            **TenantSerializer(tenant).data,
            "is_branch": tenant.parent_id is not None,
            "is_main": tenant.is_main,
            "is_root": tenant.parent_id is None,
            "parent_name": tenant.parent.name if tenant.parent else None,
            "family": [
                {
                    "id": member.id,
                    "tin": member.tin,
                    "name": member.name,
                    "is_main": member.is_main,
                    "is_root": member.parent_id is None,
                    # Satellite branches display the main branch's logo.
                    "logo_url": member.logo_url,
                }
                for member in family
            ],
            # Yearly billing: the renewal falls due at the end of the coverage.
            "next_renewal_date": tenant.coverage_ends_on,
            "paid_years": paid_years,
            "setup_approved": setup_holder.setup_payments.filter(
                status="APPROVED"
            ).exists(),
            "counts": {
                "staff": tenant.staff.count(),
                "branches": tenant.branches.count(),
                "lots": lots.count(),
                "payments": tenant.subscription_payments.count(),
                "setup_payments": tenant.setup_payments.count(),
                "roles": roles,
            },
            "stock_value": str(stock_value),
            "owner_present": bool(roles.get("OWNER")),
            "chat": {
                "thread": thread.id if thread else None,
                "status": thread.status if thread else None,
                "subject": thread.subject if thread else "",
                "unread_for_apex": thread.messages.filter(
                    side="TENANT", read_by_apex=False
                ).count()
                if thread
                else 0,
                "messages": thread.messages.count() if thread else 0,
            },
        }
        payload.update(access_payload(tenant))
        payload.update(billing_payload(tenant))
        payload["modules"] = module_flags(tenant)["modules"]
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

        def bad(message):
            return Response({"errors": {"message": message}}, status=400)

        for field in ("name", "phone", "address", "logo_url", "subscription_note"):
            if field in request.data:
                setattr(tenant, field, str(request.data[field] or "").strip())
        logo_url = str(request.data.get("logo_url") or "").strip()
        if logo_url and not logo_url.startswith(("https://", "http://")):
            return bad("The logo must be an uploaded image URL.")

        for field in ("module_inventory", "module_hr", "sales_role_enabled"):
            if field in request.data:
                setattr(tenant, field, truthy(request.data[field]))

        if "vat_rate" in request.data:
            try:
                tenant.vat_rate = Decimal(str(request.data["vat_rate"] or "0"))
            except InvalidOperation:
                return bad("vat_rate must be a number.")

        if "annual_fee" in request.data:
            try:
                tenant.annual_fee = Decimal(str(request.data["annual_fee"] or "0"))
            except InvalidOperation:
                return bad("annual_fee must be a number.")
            # A typed-in fee stops the catalog from repricing this tenant.
            tenant.fees_manually_set = True

        if "subscription_expires_on" in request.data:
            raw = str(request.data["subscription_expires_on"] or "").strip()
            if raw in ("", "null"):
                tenant.subscription_expires_on = None
            else:
                expires_on = parse_date(raw[:10])
                if expires_on is None:
                    return bad("subscription_expires_on must be a YYYY-MM-DD date.")
                tenant.subscription_expires_on = expires_on

        if "sales_agent" in request.data:
            agent_id = str(request.data["sales_agent"] or "").strip()
            if agent_id in ("", "null"):
                tenant.sales_agent = None
            else:
                tenant.sales_agent = PlatformSalesAgent.objects.filter(pk=agent_id).first()
                if tenant.sales_agent is None:
                    return bad("That sales agent does not exist.")

        tenant.save()

        _suggested_plan, suggested, suggested_setup = suggest_fees(tenant)
        apply_fee = truthy(request.data.get("apply_fee", False))
        fee_applied = False
        if apply_fee and suggested is not None:
            tenant.annual_fee = suggested
            tenant.setup_fee = suggested_setup
            tenant.fees_manually_set = False
            tenant.save(
                update_fields=["annual_fee", "setup_fee", "fees_manually_set"]
            )
            fee_applied = True

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
            .select_related(
                "tenant",
                "tenant__sales_agent",
                "tenant__parent",
                "tenant__parent__sales_agent",
                "submitted_by",
                "decided_by",
                "recorded_by",
            )
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
        from .console_views import staff_row
        from user.models import Profile

        profiles = (
            Profile.objects.filter(tenant=tenant)
            .select_related("user", "tenant", "login_disabled_by")
            .order_by("role", "user__username")
        )
        term = (request.query_params.get("search") or "").strip()
        if term:
            profiles = profiles.filter(
                Q(user__username__icontains=term)
                | Q(user__first_name__icontains=term)
                | Q(user__last_name__icontains=term)
                | Q(phone__icontains=term)
            )
        role = (request.query_params.get("role") or "").upper()
        if role:
            profiles = profiles.filter(role=role)
        rows = [staff_row(profile) for profile in profiles]
        if (request.query_params.get("disabled") or "").lower() in ("1", "true", "yes"):
            rows = [row for row in rows if row["login_disabled"]]
        return Response({"data": rows, "meta": {"total": len(rows)}})


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
    DELETE → removes this tenant's owner login.
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

        log(
            request.user,
            "OWNER_UPDATED",
            "user",
            owner.pk,
            f"@{owner.username} email={owner.email}",
        )
        return Response({"data": _owner_payload(owner)})

    @require_platform_admin
    def delete(self, request, pk):
        from user.models import Profile

        tenant, owner = self._owner(pk)
        if not tenant:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if not owner:
            return Response(
                {"errors": {"message": "This tenant has no owner account."}}, status=404
            )
        owner_id, owner_name = owner.pk, owner.username
        Profile.objects.filter(user=owner, tenant=tenant).delete()
        if not Profile.objects.filter(user=owner).exists():
            owner.delete()

        log(request.user, "OWNER_DELETED", "user", owner_id, owner_name)
        return Response({"data": None})


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
        from .console_views import status_counts
        from user.models import Profile

        today = timezone.localdate()
        tenants = Tenant.objects.all()
        companies = tenants.filter(parent__isnull=True).exclude(account_status="DELETED")
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
        owner_tenant_ids = set(
            Profile.objects.filter(role="OWNER").values_list("tenant_id", flat=True)
        )
        threads = ChatThread.objects.all()
        unread_messages = ChatMessage.objects.filter(side="TENANT", read_by_apex=False)
        # Post-year billing: a null expiry is covered for its first year.
        year_start = today - timedelta(days=365)
        covered_null = Q(subscription_expires_on__isnull=True, created_at__date__gte=year_start)
        lapsed_null = Q(subscription_expires_on__isnull=True, created_at__date__lt=year_start)
        due_overdue = companies.filter(Q(subscription_expires_on__lt=today) | lapsed_null)
        return Response(
            {
                "data": {
                    "companies": companies.count(),
                    "branches": tenants.filter(parent__isnull=False).count(),
                    "active": companies.filter(
                        Q(subscription_expires_on__gte=today) | covered_null
                    ).count(),
                    "due_soon": companies.filter(
                        Q(
                            subscription_expires_on__gte=today,
                            subscription_expires_on__lte=soon,
                        )
                        | Q(
                            subscription_expires_on__isnull=True,
                            created_at__date__gte=year_start,
                            created_at__date__lte=soon - timedelta(days=365),
                        )
                    ).count(),
                    "expired": due_overdue.count(),
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
                    "unread_feedback": Feedback.objects.filter(unread=True).count(),
                    # --- console health (the header pill + queue tiles) -----
                    "suspended": tenants.filter(account_status="SUSPENDED").count(),
                    "banned": tenants.filter(account_status="BANNED").count(),
                    "deleted": tenants.filter(account_status="DELETED").count(),
                    "without_owner": companies.exclude(
                        pk__in=owner_tenant_ids
                    ).count(),
                    "logins_disabled": Profile.objects.filter(
                        login_disabled=True
                    ).count(),
                    "illustrations": tenants.filter(is_illustration=True).count(),
                    "on_billing_hold": tenants.filter(billing_hold=True).count(),
                    "trial_ending": tenants.filter(
                        free_trial_ends_on__gte=today,
                        free_trial_ends_on__lte=today + timedelta(days=5),
                    ).count(),
                    "overdue": due_overdue.count(),
                    # Trials that ran out — the console's "expired trial" tile.
                    "trial_expired": tenants.filter(
                        free_trial_ends_on__lt=today,
                        is_illustration=False,
                        billing_hold=False,
                    ).count(),
                    "open_threads": threads.filter(status="OPEN").count(),
                    "unread_messages": unread_messages.count(),
                    "threads_with_unread": threads.filter(
                        messages__side="TENANT", messages__read_by_apex=False
                    ).distinct().count(),
                    "new_this_month": tenants.filter(
                        created_at__date__gte=today.replace(day=1)
                    ).count(),
                    "subscription_statuses": status_counts(payments),
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
        params = request.query_params
        rows = SubscriptionPayment.objects.select_related(
            "tenant",
            "tenant__sales_agent",
            "tenant__parent",
            "tenant__parent__sales_agent",
            "recorded_by",
            "submitted_by",
            "decided_by",
        )
        status_filter = (params.get("status") or "").upper()
        if status_filter in {"PENDING", "APPROVED", "REJECTED"}:
            rows = rows.filter(status=status_filter)
        channel = (params.get("channel") or "").upper()
        if channel in {"TELEBIRR", "CBE", "BANK", "CASH", "OTHER"}:
            rows = rows.filter(channel=channel)
        tenant_id = (params.get("tenant") or "").strip()
        if tenant_id.isdigit():
            rows = rows.filter(tenant_id=int(tenant_id))
        term = (params.get("search") or "").strip()
        if term:
            rows = rows.filter(
                Q(transaction_id__icontains=term) | Q(tenant__name__icontains=term)
            )
        try:
            limit = max(1, min(int(params.get("limit") or 200), 500))
            offset = max(0, int(params.get("offset") or 0))
        except ValueError:
            limit, offset = 200, 0
        total = rows.count()
        rows = rows.order_by("-created_at", "-id")[offset : offset + limit]
        return Response(
            {"data": [payment_row(row) for row in rows], "meta": {"total": total}}
        )

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
        if channel not in {"TELEBIRR", "CBE", "BANK", "CASH", "OTHER"}:
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

            log(
                request.user,
                "SUBSCRIPTION_RECORDED",
                "tenant",
                tenant.pk,
                transaction_id,
            )
        else:

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

    `id` is the USER id (the reset-password and login-toggle endpoints target
    it); `profile_id` is kept for reference.

    ?search= username/name/phone/tenant · ?role= STORE|FINANCE|MANAGER|HR|
    SALES|OWNER · ?tenant= <id> · ?disabled= true
    """

    @require_platform_admin
    def get(self, request):
        from .console_views import staff_row
        from user.models import Profile

        profiles = (
            Profile.objects.select_related("user", "tenant", "login_disabled_by")
            .exclude(tenant__account_status="DELETED")
            .order_by("tenant__name", "role")
        )
        params = request.query_params
        term = (params.get("search") or "").strip()
        if term:
            profiles = profiles.filter(
                Q(user__username__icontains=term)
                | Q(user__first_name__icontains=term)
                | Q(user__last_name__icontains=term)
                | Q(user__email__icontains=term)
                | Q(phone__icontains=term)
                | Q(tenant__name__icontains=term)
            )
        role = (params.get("role") or "").upper()
        if role:
            profiles = profiles.filter(role=role)
        tenant_id = (params.get("tenant") or "").strip()
        if tenant_id == "none":
            profiles = profiles.filter(tenant__isnull=True)
        elif tenant_id:
            profiles = profiles.filter(tenant_id=tenant_id)
        rows = [staff_row(profile) for profile in profiles]
        if (params.get("disabled") or "").lower() in ("1", "true", "yes"):
            rows = [row for row in rows if row["login_disabled"]]
        return Response({"data": rows, "meta": {"total": len(rows)}})


class BranchCreateView(APIView):
    """Child tenants of one company: GET the list, POST to create one."""

    @require_platform_admin
    def get(self, request, pk):
        member = Tenant.objects.filter(pk=pk).first()
        if not member:
            return Response({"errors": {"message": "Not found."}}, status=404)
        # No company root: every member of the family is a branch. The record
        # created with the tenant is the main branch until another is promoted,
        # and stays on the list as a satellite afterwards.
        root = member.family_root
        branches = (
            Tenant.objects.filter(Q(pk=root.pk) | Q(parent=root))
            .annotate(staff_count=Count("staff", distinct=True))
            .order_by("-is_main", "name")
        )
        return Response(
            {
                "data": [
                    {
                        **TenantSerializer(branch).data,
                        "is_branch": branch.parent_id is not None,
                        "is_root": branch.pk == root.pk,
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

        # A branch can price a different module mix than its company, so the
        # console may send its own switches. Anything omitted inherits.
        module_inventory = (
            truthy(request.data["module_inventory"])
            if "module_inventory" in request.data
            else parent.module_inventory
        )
        module_hr = (
            truthy(request.data["module_hr"])
            if "module_hr" in request.data
            else parent.module_hr
        )
        sales_role_enabled = (
            truthy(request.data["sales_role_enabled"])
            if "sales_role_enabled" in request.data
            else parent.sales_role_enabled
        )
        _plan, yearly, setup = fees_for_modules(
            module_inventory, module_hr, sales_role_enabled
        )

        branch = Tenant.objects.create(
            name=name,
            tin=tin,
            phone=request.data.get("phone", ""),
            address=request.data.get("address", ""),
            vat_rate=parent.vat_rate,
            module_inventory=module_inventory,
            module_hr=module_hr,
            sales_role_enabled=sales_role_enabled,
            annual_fee=yearly if yearly is not None else Decimal("0.00"),
            # Modules drive the catalog fee; a missing catalog price stays at
            # zero so the console shows its missing-fee warning instead of
            # borrowing another plan's amount.
            setup_fee=setup if setup is not None else Decimal("0.00"),
            parent=parent,
            is_illustration=parent.family_is_illustration,
            logo_url=parent.family_logo_url,
            subscription_expires_on=timezone.localdate() + timedelta(days=365),
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


        log(request.user, "BRANCH_CREATED", "tenant", branch.pk, branch.name)
        return Response({"data": TenantSerializer(branch).data}, status=201)
