from decimal import Decimal

from rest_framework import serializers

from .models import (
    Item,
    RegistrationLine,
    RegistrationRequest,
    StockAssignment,
    StockLot,
    StockMovement,
    StockOutRequest,
)


class ItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = Item
        fields = ["id", "name", "category", "unit", "note"]


class StockLotSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    category = serializers.CharField(source="item.category", read_only=True)
    unit = serializers.CharField(source="item.unit", read_only=True)
    price_with_vat = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    paid_amount = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    stocked_out_quantity = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True)

    class Meta:
        model = StockLot
        fields = [
            "id",
            "voucher_number",
            "item",
            "item_name",
            "category",
            "unit",
            "quantity",
            "remaining_quantity",
            "stocked_out_quantity",
            "unit_cost",
            "unit_sales",
            "vat_rate",
            "price_with_vat",
            "paid_amount",
            "registration_date",
            "expiry_date",
            "supplier_name",
            "supplier_phone",
            "supplier_address",
            "supplier_tin",
            "note",
        ]


class RegistrationLineSerializer(serializers.ModelSerializer):
    price_with_vat = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    paid_amount = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)

    class Meta:
        model = RegistrationLine
        fields = [
            "id",
            "item_name",
            "category",
            "unit",
            "quantity",
            "unit_cost",
            "unit_sales",
            "vat_rate",
            "price_with_vat",
            "paid_amount",
            "registration_date",
            "expiry_date",
            "note",
        ]
        extra_kwargs = {"vat_rate": {"required": False}}

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than zero.")
        return value


class RegistrationSerializer(serializers.ModelSerializer):
    lines = RegistrationLineSerializer(many=True)
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True)
    total_value = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)

    class Meta:
        model = RegistrationRequest
        fields = [
            "id",
            "voucher_number",
            "status",
            "supplier_name",
            "supplier_phone",
            "supplier_address",
            "supplier_tin",
            "note",
            "created_at",
            "created_by_name",
            "total_value",
            "lines",
        ]
        read_only_fields = ["voucher_number", "status", "created_at"]

    def validate_lines(self, lines):
        if not lines:
            raise serializers.ValidationError("At least one line is required.")
        return lines


class StockAssignmentSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="lot.item.name", read_only=True)
    voucher_number = serializers.CharField(source="lot.voucher_number", read_only=True)
    agent_name = serializers.CharField(source="sales_agent.full_name", read_only=True)

    class Meta:
        model = StockAssignment
        fields = [
            "id",
            "lot",
            "voucher_number",
            "item_name",
            "sales_agent",
            "agent_name",
            "quantity",
            "changing_amount",
            "change_reason",
            "unit_sales_at_assignment",
        ]
        read_only_fields = ["unit_sales_at_assignment"]

    def validate(self, attrs):
        lot = attrs.get("lot")
        quantity = attrs.get("quantity") or Decimal("0")
        changing = attrs.get("changing_amount") or Decimal("0")
        reason = attrs.get("change_reason", "")

        if quantity <= 0:
            raise serializers.ValidationError({"quantity": "Quantity must be greater than zero."})
        if lot and quantity > lot.remaining_quantity:
            raise serializers.ValidationError(
                {"quantity": f"{lot.voucher_number} only has {lot.remaining_quantity} available."}
            )
        if changing != 0 and not reason.strip():
            raise serializers.ValidationError(
                {"change_reason": "A reason is required when the changing amount is not zero."}
            )
        if lot and lot.unit_sales + changing <= 0:
            raise serializers.ValidationError(
                {"changing_amount": "The effective price must stay above zero."}
            )
        return attrs


class StockOutSerializer(serializers.ModelSerializer):
    assignments = StockAssignmentSerializer(many=True)
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True)

    class Meta:
        model = StockOutRequest
        fields = [
            "id",
            "voucher_number",
            "mode",
            "date",
            "status",
            "note",
            "created_at",
            "created_by_name",
            "assignments",
        ]
        read_only_fields = ["voucher_number", "status", "created_at"]

    def validate_assignments(self, assignments):
        if not assignments:
            raise serializers.ValidationError("At least one assignment is required.")
        return assignments


class MovementSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="lot.item.name", read_only=True)
    voucher_number = serializers.CharField(source="lot.voucher_number", read_only=True)
    actor_name = serializers.CharField(source="actor.username", read_only=True)

    class Meta:
        model = StockMovement
        fields = [
            "id",
            "type",
            "lot",
            "voucher_number",
            "item_name",
            "quantity_delta",
            "actor_name",
            "role",
            "ref_type",
            "ref_id",
            "note",
            "at",
        ]


class LotEditSerializer(serializers.ModelSerializer):
    """Manager-only inventory edit: details + prices + the Quantity field."""

    item_name = serializers.CharField(source="item.name", required=False)

    class Meta:
        model = StockLot
        fields = [
            "id",
            "item_name",
            "unit_sales",
            "unit_cost",
            "remaining_quantity",
            "expiry_date",
            "note",
            "supplier_name",
            "supplier_phone",
            "supplier_address",
            "supplier_tin",
        ]

    def validate_remaining_quantity(self, value):
        if value < 0:
            raise serializers.ValidationError("Quantity cannot be negative.")
        return value

    def update(self, instance, validated_data):
        item_name = validated_data.pop("item_name", None)
        if item_name:
            instance.item.name = item_name
            instance.item.save(update_fields=["name"])
        return super().update(instance, validated_data)
