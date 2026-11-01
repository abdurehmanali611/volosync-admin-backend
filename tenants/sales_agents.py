"""Apex's own sales agents — platform-level, NOT the tenant's `sales` app."""

from rest_framework.response import Response
from rest_framework.views import APIView

from user.platform import require_platform_admin

from .models import PlatformSalesAgent
from .platform_views import log
from .serializers import PlatformSalesAgentSerializer


class PlatformSalesAgentListCreateView(APIView):
    @require_platform_admin
    def get(self, request):
        rows = PlatformSalesAgent.objects.all()
        return Response({"data": PlatformSalesAgentSerializer(rows, many=True).data})

    @require_platform_admin
    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"errors": {"message": "Agent name is required."}}, status=400)
        agent = PlatformSalesAgent.objects.create(
            name=name,
            phone=request.data.get("phone", ""),
            email=request.data.get("email", ""),
            sex=request.data.get("sex", ""),
            active=bool(request.data.get("active", True)),
            notes=request.data.get("notes", ""),
        )
        log(request.user, "PLATFORM_SALES_AGENT_CREATED", "platform_sales_agent", agent.pk, agent.name)
        return Response({"data": PlatformSalesAgentSerializer(agent).data}, status=201)


class PlatformSalesAgentDetailView(APIView):
    @require_platform_admin
    def patch(self, request, pk):
        agent = PlatformSalesAgent.objects.filter(pk=pk).first()
        if agent is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        allowed = ["name", "phone", "email", "sex", "active", "notes"]
        for field in allowed:
            if field in request.data:
                setattr(agent, field, request.data[field])
        agent.save()
        log(request.user, "PLATFORM_SALES_AGENT_UPDATED", "platform_sales_agent", agent.pk, agent.name)
        return Response({"data": PlatformSalesAgentSerializer(agent).data})
