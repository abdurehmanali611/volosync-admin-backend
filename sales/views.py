from decimal import Decimal

from django.contrib.auth import authenticate, get_user_model
from django.db.models import Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from approvals.services import decide
from inventory.views import require_roles, role_of, tenant_of

from .models import SalesAgent, SalesSettlement
from .serializers import SalesAgentSerializer, SalesSettlementSerializer

User = get_user_model()


class SalesAgentListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        agents = SalesAgent.objects.filter(tenant=tenant)
        return Response({"data": SalesAgentSerializer(agents, many=True).data})

    @require_roles("MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        data = request.data
        password = data.get("password") or "volosync123"
        username = data.get("login_username") or f"sales-{tenant.tin.lower()}"
        user = User.objects.create_user(
            username=f"agent-{timezone.now().timestamp()}",
            password=password,
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
        )
        agent = SalesAgent.objects.create(
            tenant=tenant,
            user=user,
            login_username=username,
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            phone=data.get("phone", ""),
            sex=data.get("sex", ""),
            years_experience=data.get("years_experience", 0) or 0,
        )
        return Response(
            {
                "data": SalesAgentSerializer(agent).data,
                "initial_password": password,
            },
            status=201,
        )


class SalesAgentLoginView(APIView):
    """Shared tenant username + the agent's own password identifies the agent."""

    permission_classes = [AllowAny]

    def post(self, request):
        username = request.data.get("username", "")
        password = request.data.get("password", "")
        candidates = SalesAgent.objects.filter(
            login_username__iexact=username, is_active=True
        ).select_related("user")
        for agent in candidates:
            if agent.user and agent.user.check_password(password):
                refresh = RefreshToken.for_user(agent.user)
                return Response(
                    {
                        "data": {
                            "access": str(refresh.access_token),
                            "refresh": str(refresh),
                            "user": {
                                "id": agent.user_id,
                                "name": agent.full_name,
                                "role": "SALES",
                                "tenant": agent.tenant.name,
                                "must_change_password": agent.must_change_password,
                            },
                        }
                    }
                )
        return Response(
            {"errors": {"message": "Invalid credentials."}},
            status=status.HTTP_401_UNAUTHORIZED,
        )


class SalesAgentRankingView(APIView):
    @require_roles("MANAGER", "FINANCE", "OWNER")
    def get(self, request):
        tenant = tenant_of(request.user)
        rows = (
            SalesSettlement.objects.filter(
                tenant=tenant, status="APPROVED"
            )
            .values("assignment__sales_agent__id", "assignment__sales_agent__first_name", "assignment__sales_agent__last_name")
            .annotate(revenue=Sum("sales_amount"), units=Sum("sold_quantity"))
            .order_by("-revenue", "-units")
        )
        data = [
            {
                "agent_id": row["assignment__sales_agent__id"],
                "name": f"{row['assignment__sales_agent__first_name']} {row['assignment__sales_agent__last_name']}",
                "revenue": str(row["revenue"] or 0),
                "units": str(row["units"] or 0),
            }
            for row in rows
        ]
        return Response({"data": data})


class SettlementListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = SalesSettlement.objects.filter(tenant=tenant).select_related(
            "assignment__sales_agent", "assignment__lot__item"
        )
        if role_of(request.user) == "SALES":
            qs = qs.filter(assignment__sales_agent__user=request.user)
        if request.query_params.get("queue") == "1":
            wanted = {"FINANCE": "PENDING_FINANCE", "MANAGER": "PENDING_MANAGER"}.get(
                role_of(request.user)
            )
            qs = qs.filter(status=wanted) if wanted else qs.none()
        return Response({"data": SalesSettlementSerializer(qs, many=True).data})

    @require_roles("FINANCE", "SALES")
    def post(self, request):
        tenant = tenant_of(request.user)
        serializer = SalesSettlementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assignment = serializer.validated_data["assignment"]
        if assignment.request.tenant_id != tenant.id:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if assignment.request.status != "APPROVED":
            return Response(
                {"errors": {"assignment": "This assignment is not approved yet."}},
                status=400,
            )
        if hasattr(assignment, "settlement"):
            return Response(
                {"errors": {"assignment": "This assignment is already settled."}},
                status=400,
            )
        unit_price = assignment.unit_sales_at_assignment + assignment.changing_amount
        sold = serializer.validated_data["sold_quantity"]
        if sold > assignment.quantity:
            return Response(
                {"errors": {"sold_quantity": "Cannot sell more than the assigned quantity."}},
                status=400,
            )
        role = role_of(request.user)
        settlement = serializer.save(
            tenant=tenant,
            returned_quantity=assignment.quantity - sold,
            expected_amount=(unit_price * sold).quantize(Decimal("0.01")),
            entered_by=request.user,
            status="PENDING_FINANCE" if role == "SALES" else "PENDING_MANAGER",
        )
        return Response({"data": SalesSettlementSerializer(settlement).data}, status=201)


class SettlementDecisionView(APIView):
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        settlement = SalesSettlement.objects.filter(tenant=tenant, pk=pk).first()
        if not settlement:
            return Response({"errors": {"message": "Not found."}}, status=404)
        decide(
            settlement,
            request.user,
            request.data.get("decision"),
            request.data.get("note", ""),
        )
        return Response({"data": SalesSettlementSerializer(settlement).data})
