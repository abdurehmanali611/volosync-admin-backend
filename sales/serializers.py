from rest_framework import serializers

from .models import SalesAgent, SalesSettlement


class SalesAgentSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="full_name", read_only=True)
    assigned_count = serializers.SerializerMethodField()

    class Meta:
        model = SalesAgent
        fields = [
            "id",
            "name",
            "login_username",
            "first_name",
            "last_name",
            "phone",
            "sex",
            "years_experience",
            "is_active",
            "must_change_password",
            "assigned_count",
        ]

    def get_assigned_count(self, obj):
        return obj.assignments.filter(request__status="APPROVED").count()


class SalesSettlementSerializer(serializers.ModelSerializer):
    agent_name = serializers.CharField(source="assignment.sales_agent.full_name", read_only=True)
    item_name = serializers.CharField(source="assignment.lot.item.name", read_only=True)
    voucher_number = serializers.CharField(source="assignment.lot.voucher_number", read_only=True)

    class Meta:
        model = SalesSettlement
        fields = [
            "id",
            "assignment",
            "agent_name",
            "item_name",
            "voucher_number",
            "sold_quantity",
            "returned_quantity",
            "payment_method",
            "sales_amount",
            "expected_amount",
            "penalty",
            "note",
            "status",
            "created_at",
        ]
        read_only_fields = ["returned_quantity", "expected_amount", "status", "created_at"]
