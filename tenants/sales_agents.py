"""Apex's own sales agents — platform-level, NOT the tenant's `sales` app."""

from django.db import models, transaction
from rest_framework.response import Response
from rest_framework.views import APIView

from user.platform import require_platform_admin

from .models import PlatformSalesAgent
from .platform_views import log
from .serializers import PlatformSalesAgentSerializer


class PlatformSalesAgentListCreateView(APIView):
    """?search= name/phone/email · ?active= true|false · ?limit · ?offset"""

    @require_platform_admin
    def get(self, request):
        from django.db.models import Count, Q

        rows = PlatformSalesAgent.objects.annotate(
            tenant_count=Count("tenants", distinct=True)
        )
        term = (request.query_params.get("search") or "").strip()
        if term:
            rows = rows.filter(
                Q(name__icontains=term)
                | Q(phone__icontains=term)
                | Q(email__icontains=term)
                | Q(notes__icontains=term)
            )
        active = (request.query_params.get("active") or "").strip().lower()
        if active in ("1", "true", "yes"):
            rows = rows.filter(active=True)
        elif active in ("0", "false", "no"):
            rows = rows.filter(active=False)
        total = rows.count()
        payload = [
            {
                **PlatformSalesAgentSerializer(row).data,
                "tenant_count": row.tenant_count,
            }
            for row in rows
        ]
        return Response({"data": payload, "meta": {"total": total}})

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
    """PATCH updates the listed fields, DELETE removes the seller.

    An agent still attached to wholesalers is not deleted by default — those
    tenants would silently lose their seller. Send ?detach=true to clear the
    links (the FK is SET_NULL) and delete anyway.
    """

    @require_platform_admin
    def get(self, request, pk):
        agent = (
            PlatformSalesAgent.objects.annotate(tenant_count=models.Count("tenants"))
            .filter(pk=pk)
            .first()
        )
        if agent is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        return Response(
            {
                "data": {
                    **PlatformSalesAgentSerializer(agent).data,
                    "tenant_count": agent.tenant_count,
                }
            }
        )

    @require_platform_admin
    def patch(self, request, pk):
        agent = PlatformSalesAgent.objects.filter(pk=pk).first()
        if agent is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        allowed = ["name", "phone", "email", "sex", "active", "notes"]
        for field in allowed:
            if field in request.data:
                value = request.data[field]
                if field == "active" and not isinstance(value, bool):
                    value = str(value).lower() in ("1", "true", "yes")
                setattr(agent, field, value)
        if "name" in request.data and not (request.data.get("name") or "").strip():
            return Response({"errors": {"message": "Agent name is required."}}, status=400)
        agent.save()
        log(request.user, "PLATFORM_SALES_AGENT_UPDATED", "platform_sales_agent", agent.pk, agent.name)
        return Response({"data": PlatformSalesAgentSerializer(agent).data})

    @require_platform_admin
    def delete(self, request, pk):
        from .models import Tenant

        agent = PlatformSalesAgent.objects.filter(pk=pk).first()
        if agent is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        attached = Tenant.objects.filter(sales_agent=agent).count()
        detach = str(request.query_params.get("detach", "")).lower() in ("1", "true", "yes")
        if attached and not detach:
            return Response(
                {
                    "errors": {
                        "message": f"This seller is on {attached} wholesaler(s). Reassign them or send ?detach=true."
                    }
                },
                status=400,
            )
        name = agent.name
        with transaction.atomic():
            if attached:
                Tenant.objects.filter(sales_agent=agent).update(sales_agent=None)
            agent.delete()
        log(
            request.user,
            "PLATFORM_SALES_AGENT_DELETED",
            "platform_sales_agent",
            pk,
            f"{name} · {attached} tenants detached",
        )
        return Response({"data": {"id": int(pk), "deleted": True, "tenants_detached": attached}})
