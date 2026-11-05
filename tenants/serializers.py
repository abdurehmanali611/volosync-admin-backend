from rest_framework import serializers

from .models import PlatformSalesAgent, PricingPlan, SubscriptionPayment, Tenant


class TenantSerializer(serializers.ModelSerializer):
    sales_agent_name = serializers.CharField(
        source="sales_agent.name", read_only=True, default=None
    )

    class Meta:
        model = Tenant
        fields = [
            "id",
            "name",
            "tin",
            "phone",
            "address",
            "logo_url",
            "vat_rate",
            "module_inventory",
            "module_hr",
            "sales_role_enabled",
            "sales_agent",
            "sales_agent_name",
            "parent",
            "is_main",
            "subscription_expires_on",
            "subscription_transaction_id",
            "subscription_paid_on",
            "subscription_note",
            "subscription_state",
            "annual_fee",
            "created_at",
        ]


class SubscriptionPaymentSerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    transaction_ref = serializers.CharField(source="transaction_id", read_only=True)

    class Meta:
        model = SubscriptionPayment
        fields = [
            "id",
            "tenant",
            "tenant_name",
            "transaction_id",
            "transaction_ref",
            "channel",
            "status",
            "amount",
            "years",
            "paid_on",
            "covers_until",
            "note",
            "submitted_by",
            "submitted_at",
            "decided_by",
            "decided_at",
            "decision_note",
        ]


class PricingPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = PricingPlan
        fields = [
            "id",
            "name",
            "setup_fee",
            "yearly_base_fee",
            "inventory_yearly_fee",
            "hr_yearly_fee",
            "sales_role_yearly_fee",
            "description",
            "is_default",
            "active",
            "created_at",
        ]


class PlatformSalesAgentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlatformSalesAgent
        fields = [
            "id",
            "name",
            "phone",
            "email",
            "sex",
            "active",
            "notes",
            "created_at",
        ]
