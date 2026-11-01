from decimal import Decimal

from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from inventory.models import next_voucher
from inventory.views import require_roles, tenant_of

from .models import Asset, AssetAssignment
from .serializers import AssetSerializer


class AssetListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        assets = Asset.objects.filter(tenant=tenant).prefetch_related("assignments")
        return Response({"data": AssetSerializer(assets, many=True).data})

    @require_roles("MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        data = request.data
        count = Asset.objects.filter(tenant=tenant).count()
        asset = Asset.objects.create(
            tenant=tenant,
            asset_tag=data.get("asset_tag") or f"AST-{timezone.now().year}-{count + 1:06d}",
            name=data.get("name", "Asset"),
            category=data.get("category", "OTHER"),
            serial_number=data.get("serial_number", ""),
            quantity=data.get("quantity") or Decimal("1"),
            brand_model=data.get("brand_model", ""),
            unit_cost=data.get("unit_cost") or 0,
            condition_at_registration=data.get("condition_at_registration", "GOOD"),
            useful_life_years=data.get("useful_life_years") or 5,
            created_by=request.user,
        )
        return Response({"data": AssetSerializer(asset).data}, status=201)


class AssetDetailView(APIView):
    @require_roles("MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        asset = Asset.objects.filter(tenant=tenant, pk=pk).first()
        if not asset:
            return Response({"errors": {"message": "Not found."}}, status=404)
        for field in [
            "name",
            "category",
            "serial_number",
            "quantity",
            "brand_model",
            "unit_cost",
            "condition_at_registration",
            "useful_life_years",
        ]:
            if field in request.data:
                setattr(asset, field, request.data[field])
        asset.save()
        return Response({"data": AssetSerializer(asset).data})


class AssetAssignView(APIView):
    @require_roles("FINANCE")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        asset = Asset.objects.filter(tenant=tenant, pk=pk).first()
        if not asset:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if asset.status == "ASSIGNED":
            return Response(
                {"errors": {"message": "This asset is already assigned."}}, status=400
            )
        assignee = (request.data.get("assignee_name") or "").strip()
        if not assignee:
            return Response(
                {"errors": {"assignee_name": "Type the assignee name."}}, status=400
            )
        AssetAssignment.objects.create(
            asset=asset,
            assignee_name=assignee,
            assigned_at=request.data.get("assigned_at") or timezone.localdate(),
            assigned_by=request.user,
        )
        asset.status = "ASSIGNED"
        asset.save(update_fields=["status"])
        return Response({"data": AssetSerializer(asset).data})


class AssetRemoveView(APIView):
    @require_roles("FINANCE")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        asset = Asset.objects.filter(tenant=tenant, pk=pk).first()
        if not asset:
            return Response({"errors": {"message": "Not found."}}, status=404)
        assignment = asset.assignments.filter(is_active=True).first()
        if not assignment:
            return Response(
                {"errors": {"message": "This asset is not assigned."}}, status=400
            )
        assignment.is_active = False
        assignment.removed_at = timezone.localdate()
        assignment.save(update_fields=["is_active", "removed_at"])
        asset.status = "AVAILABLE"
        asset.save(update_fields=["status"])
        return Response({"data": AssetSerializer(asset).data})
