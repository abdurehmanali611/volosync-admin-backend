from rest_framework import serializers

from .models import Asset, AssetAssignment


class AssetAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetAssignment
        fields = ["id", "assignee_name", "assigned_at", "is_active", "removed_at"]


class AssetSerializer(serializers.ModelSerializer):
    assignee_name = serializers.CharField(read_only=True)
    book_value = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    assignments = AssetAssignmentSerializer(many=True, read_only=True)

    class Meta:
        model = Asset
        fields = [
            "id",
            "asset_tag",
            "name",
            "category",
            "serial_number",
            "quantity",
            "brand_model",
            "unit_cost",
            "condition_at_registration",
            "useful_life_years",
            "status",
            "assignee_name",
            "book_value",
            "created_at",
            "assignments",
        ]
