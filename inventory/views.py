from functools import wraps
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from approvals.services import decide, initial_status
from sales.models import SalesAgent

from .models import (
    Item,
    RegistrationLine,
    RegistrationRequest,
    StockAssignment,
    StockLot,
    StockMovement,
    StockOutRequest,
    next_voucher,
)
from .serializers import (
    LotEditSerializer,
    MovementSerializer,
    RegistrationSerializer,
    StockLotSerializer,
    StockOutSerializer,
)


def role_of(user):
    return getattr(getattr(user, "profile", None), "role", None)


def tenant_of(user):
    return getattr(getattr(user, "profile", None), "tenant", None)


def require_roles(*roles):
    def decorator(view):
        original = view

        @wraps(view)
        def wrapper(self, request, *args, **kwargs):
            if role_of(request.user) not in roles:
                raise PermissionDenied(f"Allowed roles: {', '.join(roles)}.")
            return original(self, request, *args, **kwargs)

        return wrapper

    return decorator


class LotListView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = StockLot.objects.filter(tenant=tenant).select_related("item")
        search = request.query_params.get("search")
        if search:
            qs = qs.filter(Q(item__name__icontains=search) | Q(voucher_number__icontains=search))
        only_active = request.query_params.get("active")
        if only_active == "1":
            qs = qs.filter(remaining_quantity__gt=0)
        return Response({"data": StockLotSerializer(qs, many=True).data})


class LotDetailView(APIView):
    """Manager-only inventory edit. Quantity is a plain field edit (spec §4.2a)."""

    @require_roles("MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        lot = StockLot.objects.filter(tenant=tenant, pk=pk).first()
        if not lot:
            return Response({"errors": {"message": "Not found."}}, status=404)
        serializer = LotEditSerializer(lot, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"data": StockLotSerializer(lot).data})


class InventorySummaryView(APIView):
    """Payment & tax: registered vs in store vs stocked out, VAT split."""

    def get(self, request):
        tenant = tenant_of(request.user)
        lots = StockLot.objects.filter(tenant=tenant).select_related("item")
        rows = []
        totals = {
            "registered_value": Decimal("0"),
            "in_store_value": Decimal("0"),
            "stocked_out_value": Decimal("0"),
            "vat": Decimal("0"),
        }
        for lot in lots:
            registered_value = (lot.quantity * lot.unit_cost).quantize(Decimal("0.01"))
            in_store_value = (lot.remaining_quantity * lot.unit_cost).quantize(Decimal("0.01"))
            stocked_out_value = registered_value - in_store_value
            vat = (lot.quantity * lot.unit_cost * lot.vat_rate / 100).quantize(Decimal("0.01"))
            totals["registered_value"] += registered_value
            totals["in_store_value"] += in_store_value
            totals["stocked_out_value"] += stocked_out_value
            totals["vat"] += vat
            rows.append(
                {
                    "id": lot.id,
                    "voucher_number": lot.voucher_number,
                    "item_name": lot.item.name,
                    "category": lot.item.category,
                    "unit": lot.item.unit,
                    "quantity": str(lot.quantity),
                    "remaining_quantity": str(lot.remaining_quantity),
                    "stocked_out_quantity": str(lot.stocked_out_quantity),
                    "unit_cost": str(lot.unit_cost),
                    "paid_amount": str(lot.paid_amount),
                    "price_with_vat": str(lot.price_with_vat),
                    "registered_value": str(registered_value),
                    "in_store_value": str(in_store_value),
                    "stocked_out_value": str(stocked_out_value),
                    "vat": str(vat),
                    "vat_rate": str(lot.vat_rate),
                }
            )
        return Response(
            {
                "data": {
                    "rows": rows,
                    "totals": {key: str(value) for key, value in totals.items()},
                }
            }
        )


class RegistrationListCreateView(APIView):
    @require_roles("STORE", "FINANCE", "MANAGER")
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = RegistrationRequest.objects.filter(tenant=tenant).prefetch_related("lines")
        if request.query_params.get("queue") == "1":
            role = role_of(request.user)
            wanted = {
                "FINANCE": "PENDING_FINANCE",
                "MANAGER": "PENDING_MANAGER",
            }.get(role)
            qs = qs.filter(status=wanted) if wanted else qs.none()
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return Response({"data": RegistrationSerializer(qs, many=True).data})

    @require_roles("STORE", "FINANCE", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role = role_of(request.user)
        doc = RegistrationRequest.objects.create(
            tenant=tenant,
            voucher_number=next_voucher(RegistrationRequest, tenant, "REG"),
            created_by=request.user,
            submitted_at=timezone.now(),
            status=initial_status(role),
            supplier_name=serializer.validated_data.get("supplier_name", ""),
            supplier_phone=serializer.validated_data.get("supplier_phone", ""),
            supplier_address=serializer.validated_data.get("supplier_address", ""),
            supplier_tin=serializer.validated_data.get("supplier_tin", ""),
            note=serializer.validated_data.get("note", ""),
        )
        for line_data in serializer.validated_data["lines"]:
            RegistrationLine.objects.create(
                request=doc,
                vat_rate=line_data.get("vat_rate", tenant.vat_rate),
                **{
                    key: value
                    for key, value in line_data.items()
                    if key != "vat_rate"
                },
            )
        if doc.status == "APPROVED":
            doc.apply_on_approval()
        return Response({"data": RegistrationSerializer(doc).data}, status=201)


class RegistrationDecisionView(APIView):
    @require_roles("FINANCE", "MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        doc = RegistrationRequest.objects.filter(tenant=tenant, pk=pk).first()
        if not doc:
            return Response({"errors": {"message": "Not found."}}, status=404)
        decide(doc, request.user, request.data.get("decision"), request.data.get("note", ""))
        return Response({"data": RegistrationSerializer(doc).data})


class StockOutListCreateView(APIView):
    @require_roles("STORE", "FINANCE", "MANAGER", "SALES")
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = StockOutRequest.objects.filter(tenant=tenant).prefetch_related(
            "assignments__lot__item", "assignments__sales_agent"
        )
        # Sales agents only ever see approved documents (their own assignments).
        if role_of(request.user) == "SALES":
            qs = qs.filter(status="APPROVED", assignments__sales_agent__user=request.user).distinct()
        if request.query_params.get("queue") == "1":
            wanted = {
                "FINANCE": "PENDING_FINANCE",
                "MANAGER": "PENDING_MANAGER",
            }.get(role_of(request.user))
            qs = qs.filter(status=wanted) if wanted else qs.none()
        return Response({"data": StockOutSerializer(qs, many=True).data})

    @require_roles("STORE")
    def post(self, request):
        tenant = tenant_of(request.user)
        serializer = StockOutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        doc = StockOutRequest.objects.create(
            tenant=tenant,
            voucher_number=next_voucher(StockOutRequest, tenant, "SO"),
            mode=serializer.validated_data.get("mode", "SINGLE"),
            date=serializer.validated_data["date"],
            note=serializer.validated_data.get("note", ""),
            created_by=request.user,
            status=initial_status(role_of(request.user)),
        )
        for row in serializer.validated_data["assignments"]:
            lot = row["lot"]
            StockAssignment.objects.create(
                request=doc,
                lot=lot,
                sales_agent=row.get("sales_agent"),
                quantity=row["quantity"],
                changing_amount=row.get("changing_amount", 0),
                change_reason=row.get("change_reason", ""),
                unit_sales_at_assignment=lot.unit_sales,
            )
        return Response({"data": StockOutSerializer(doc).data}, status=201)


class StockOutDecisionView(APIView):
    @require_roles("FINANCE", "MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        doc = StockOutRequest.objects.filter(tenant=tenant, pk=pk).first()
        if not doc:
            return Response({"errors": {"message": "Not found."}}, status=404)
        decide(doc, request.user, request.data.get("decision"), request.data.get("note", ""))
        return Response({"data": StockOutSerializer(doc).data})


class MovementListView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = StockMovement.objects.filter(tenant=tenant).select_related("lot__item", "actor")[:200]
        return Response({"data": MovementSerializer(qs, many=True).data})


class AgentOptionsView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        agents = SalesAgent.objects.filter(tenant=tenant, is_active=True)
        return Response(
            {
                "data": [
                    {"id": agent.id, "name": agent.full_name, "phone": agent.phone}
                    for agent in agents
                ]
            }
        )


class DashboardView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        lots = StockLot.objects.filter(tenant=tenant)
        stock_value = sum(
            (lot.remaining_quantity * lot.unit_cost for lot in lots), Decimal("0")
        )
        pending = {
            "registrations": RegistrationRequest.objects.filter(
                tenant=tenant, status__in=["PENDING_FINANCE", "PENDING_MANAGER"]
            ).count(),
            "stockouts": StockOutRequest.objects.filter(
                tenant=tenant, status__in=["PENDING_FINANCE", "PENDING_MANAGER"]
            ).count(),
        }
        return Response(
            {
                "data": {
                    "tenant": {
                        "name": tenant.name if tenant else "",
                        "tin": tenant.tin if tenant else "",
                        "vat_rate": str(tenant.vat_rate) if tenant else "15.00",
                        "sales_role_enabled": tenant.sales_role_enabled if tenant else False,
                    },
                    "role": role_of(request.user),
                    "lots": lots.count(),
                    "stock_value": str(stock_value.quantize(Decimal("0.01"))),
                    "pending": pending,
                }
            }
        )
