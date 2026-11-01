from decimal import Decimal

from django.conf import settings
from django.db import models

from tenants.models import Tenant

MONEY = {"max_digits": 18, "decimal_places": 2}
QTY = {"max_digits": 14, "decimal_places": 3}


class SalesAgent(models.Model):
    """Shared tenant username, per-agent password (spec Q7)."""

    SEX = (("F", "Female"), ("M", "Male"))

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="sales_agents"
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.CASCADE,
        related_name="sales_agent",
    )
    login_username = models.CharField(max_length=80)
    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80)
    phone = models.CharField(max_length=40, blank=True)
    sex = models.CharField(max_length=1, choices=SEX, blank=True)
    years_experience = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    must_change_password = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["first_name", "last_name"]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class SalesSettlement(models.Model):
    entity_type = "SETTLEMENT"
    METHODS = (("TELEBIRR", "Telebirr"), ("BANK", "Bank"), ("RECEIPT", "Receipt"))

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="settlements"
    )
    assignment = models.OneToOneField(
        "inventory.StockAssignment", on_delete=models.PROTECT, related_name="settlement"
    )
    sold_quantity = models.DecimalField(**QTY)
    returned_quantity = models.DecimalField(**QTY, default=Decimal("0"))
    payment_method = models.CharField(max_length=20, choices=METHODS)
    sales_amount = models.DecimalField(**MONEY)
    expected_amount = models.DecimalField(**MONEY)
    penalty = models.BooleanField(default=False)
    note = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        default="PENDING_FINANCE",
        choices=(
            ("PENDING_FINANCE", "Pending finance"),
            ("PENDING_MANAGER", "Pending manager"),
            ("APPROVED", "Approved"),
            ("REJECTED", "Rejected"),
        ),
    )
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def apply_on_approval(self):
        from inventory.models import StockMovement, StockLot

        assignment = self.assignment
        lot = StockLot.objects.select_for_update().get(pk=assignment.lot_id)
        sold = self.sold_quantity
        returned = self.returned_quantity
        if returned:
            lot.remaining_quantity += returned
            lot.save(update_fields=["remaining_quantity"])
            StockMovement.objects.create(
                tenant=self.tenant,
                lot=lot,
                type="SALE_RETURN",
                quantity_delta=returned,
                actor=self.entered_by,
                role="FINANCE",
                ref_type=self.entity_type,
                ref_id=self.pk,
                note="Unsold stock returned",
            )
        StockMovement.objects.create(
            tenant=self.tenant,
            lot=lot,
            type="SALE",
            quantity_delta=-sold,
            actor=self.entered_by,
            role="FINANCE",
            ref_type=self.entity_type,
            ref_id=self.pk,
            note=self.payment_method,
        )
